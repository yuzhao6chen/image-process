"""pdf-inspector only. Evidence retains original text-item offsets and coordinates."""
import json
import re
from pathlib import Path
from statistics import median
import pdf_inspector


def clean(s):
    return re.sub(r'\s+', '', s or '')


class View:
    def __init__(self, nodes):
        self.nodes=nodes;self.text='';self.positions=[];self.starts=[]
        for n in nodes:
            self.starts.append((len(self.text),n))
            for offset,c in enumerate(n['text']):
                if not c.isspace():self.text+=c;self.positions.append((n,offset))

    def evidence(self,a,b):
        es=[]
        for n,offset in self.positions[a:b]:
            if es and es[-1]['item_id']==n['item_id']:es[-1]['end']=offset+1
            else:es.append(dict(item_id=n['item_id'],page=n['page'],start=offset,end=offset+1,
                               section=n.get('section',''),bbox=[n['x'],n['y'],n['width'],n['height']]))
        byid={n['item_id']:n for n in self.nodes}
        for e in es:e['text']=byid[e['item_id']]['text'][e['start']:e['end']]
        return es

    def capture(self,a,b,label='',rule=''):
        es=self.evidence(a,b)
        return dict(value=self.text[a:b],page=sorted({e['page'] for e in es}),evidence=es,label=label,rule=rule)

    def find(self,pattern,label='',rule='',flags=0):
        return [self.capture(m.start('v'),m.end('v'),label,rule) for m in re.finditer(pattern,self.text,flags)]


class Document:
    def __init__(self,pdf,output):
        raw=pdf_inspector.extract_text_with_positions(str(pdf))
        count=pdf_inspector.detect_pdf(str(pdf)).page_count
        self.items=[{**{k:getattr(n,k) for k in ['page','text','x','y','width','height','font_size','item_type']},
                     'item_id':f'p{n.page}_t{i}'} for i,n in enumerate(raw)]
        # Link annotations may appear as a second standalone URL item out of reading
        # order.  Drop only those whose URL already occurs in another item on the page.
        duplicates={(n['page'],clean(n['text'])) for n in self.items if re.fullmatch(r'https?://\S+',clean(n['text']))
                    and any(x['page']==n['page'] and x is not n and clean(n['text']) in clean(x['text']) for x in self.items)}
        self.nodes=[n for n in self.items if not n['text'].startswith('[Image:')
                    and not(n['y']<80 and re.fullmatch(r'\d+|https?://\S+',clean(n['text'])))
                    and (n['page'],clean(n['text'])) not in duplicates]
        self.pages=[dict(pdf_page=p,text='\n'.join(n['text'] for n in self.nodes if n['page']==p),
                         image_count=sum(n['text'].startswith('[Image:') for n in self.items if n['page']==p)) for p in range(1,count+1)]
        self.groups,self.section_notes=self.locate()
        for n in self.items:n['section']=next((s for s,ns in self.groups.items() if n['page'] in ns),'other')
        self.all=View(self.nodes)
        self.announcement=View([n for n in self.nodes if n['page'] in self.groups['announcement']])
        self.front=View([n for n in self.nodes if n['page'] in self.groups['front']])
        self.general=View([n for n in self.nodes if n['page'] in self.groups['general']])
        self.evaluation=View([n for n in self.nodes if n['page'] in self.groups['evaluation']])
        # A table's value column is estimated from long text runs on that page, not sample coordinates.
        content=[];self.column_thresholds={}
        for p in self.groups['front']:
            nodes=[n for n in self.nodes if n['page']==p]
            xs=[n['x'] for n in nodes if len(clean(n['text']))>25]
            threshold=median(xs)-20 if xs else 0
            self.column_thresholds[p]=threshold
            content += [n for n in nodes if n['x']>=threshold]
        self.front_content=View(content)
        self.clauses=[]
        starts=[]
        for a,n in self.announcement.starts:
            m=re.match(r'^(\d+\.\d+)\s*',n['text'])
            if m:starts.append((a,m[1],a+len(clean(m[0]))))
        for i,(a,num,b) in enumerate(starts):
            end=starts[i+1][0] if i+1<len(starts) else len(self.announcement.text)
            section=re.search(r'[一二三四五六七八九十]+、(?:项目概况|投标人资格要求|申请人资格要求|招标文件|资格预审文件|预审文件|投标文件|开标时间|其他公告内容|监督部门|联系方式)',self.announcement.text[b:end])
            if section:end=b+section.start()
            self.clauses.append(dict(number=num,start=b,end=end,text=self.announcement.text[b:end]))
        output.mkdir(parents=True,exist_ok=True)
        self.write(output/'position_items.json',self.items)
        self.write(output/'pages.json',self.pages)
        self.write(output/'sections.json',dict(groups=self.groups,notes=self.section_notes,table_content_thresholds=self.column_thresholds))
        (output/'pages.txt').write_text('\n\n'.join(f'===== PDF第{p["pdf_page"]}页 =====\n{p["text"]}' for p in self.pages),encoding='utf-8')
        md=pdf_inspector.extract_pages_markdown(str(pdf))
        (output/'document.md').write_text('\n\n'.join(f'<!-- PDF PAGE {p.page+1} -->\n{p.markdown}' for p in md.pages),encoding='utf-8')

    @staticmethod
    def write(path,obj):path.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')

    def locate(self):
        texts={p['pdf_page']:clean(p['text']) for p in self.pages}
        # Use body semantics rather than printed TOC page numbers.  Both the goods and
        # construction samples contain the same business sections with different titles.
        ann=[p for p,t in texts.items() if re.search(r'项目概况(?:与|及)?招标范围|项目名称[：:]',t)
             and re.search(r'投标人资格要求|申请人资格要求',t) and re.search(r'招标条件|本招标项目',t)
             and not re.search(r'\.{4}',t)]
        if not ann:raise ValueError('公告结构未定位，不能安全抽取')
        a=min(ann)
        front=next((p for p,t in texts.items() if p>a and '1.1.2' in t and '1.1.3' in t and '1.4.1' in t),None)
        general=next((p for p,t in texts.items() if front and p>front and
                      (('1.1.1根据《' in t and '见投标人须知前附表' in t) or
                       ('1.总则' in t and '投标人须知前附表' in t))),None)
        ev=next((p for p,t in texts.items() if general and p>general and '2.1.1' in t and '2.1.2' in t and '投标人名称' in t and '营业执照' in t),None)
        contract=next((p for p,t in texts.items() if ev and p>ev and
                       ('《建设工程施工合同（示范文本）》' in t or '合同条款及格式' in t)),None)
        if None in [front,general,ev,contract]:raise ValueError('章节语义锚点不完整，请人工确认；不套用旧样本页码')
        return dict(announcement=list(range(a,front)),front=list(range(front,general)),general=list(range(general,ev)),evaluation=list(range(ev,contract))),[
            '以条款组合与引用语句识别章节正文边界，未使用固定物理页码。',
            '图片标题未OCR；目录与实际内容可能不一致，使用可读正文锚点定位。']

    def clause_fields(self,labels,rule):
        out=[]
        for c in self.clauses:
            for label in labels:
                m=re.match(re.escape(label)+r'[：:](?P<v>.+)',c['text'])
                if m:out.append(self.announcement.capture(c['start']+m.start('v'),c['start']+m.end('v'),label,rule))
        return out

    def row_values(self,label):
        out=[]
        for n in self.nodes:
            if n['page'] not in self.groups['front'] or clean(n['text'])!=label:continue
            right=[x for x in self.nodes if x['page']==n['page'] and x['x']>n['x']+n['width']+3 and abs(x['y']-n['y'])<5]
            if not right:continue
            view=View(sorted(right,key=lambda x:x['x']))
            out.append(view.capture(0,len(view.text),label,'同一表格行、右侧内容列'))
        return out
