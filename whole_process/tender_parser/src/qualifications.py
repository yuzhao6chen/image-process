"""Admission requirements from announcement clauses and the front-table value cell."""
from hashlib import sha256
import re


PREFIX = re.compile(r'^(?:资质要求|财务要求|信誉要求|其他要求)[：:]')


def classify(text):
    if re.search(r'联合体', text): return 'CONSORTIUM', '联合体要求', 'consortium'
    if re.search(r'单位负责人|控股|管理关系', text): return 'RELATIONSHIP', '负责人及控股管理关系限制', 'relationship'
    if re.search(r'失信被执行人|严重违法失信', text): return 'CREDIT', '信用与失信限制', 'credit'
    if re.search(r'财务|破产|审计报告', text): return 'FINANCIAL', '财务状况及审计要求', 'financial'
    qname=re.search(r'([\u4e00-\u9fff]{2,16}(?:施工总承包|专业承包))',text)
    if qname:return 'PROFESSIONAL_QUALIFICATION','企业专业资质','qualification:'+qname[1]
    if re.search(r'项目经理|技术负责人|建造师',text):return 'PERSONNEL','项目人员要求','personnel:'+re.sub(r'\W','',text)[:30]
    if re.search(r'业绩',text):return 'PERFORMANCE','准入业绩要求','performance:'+re.sub(r'\W','',text)[:30]
    if re.search(r'营业执照|境内注册|实施能力|施工能力',text):return 'BASIC','基本资格与实施能力','basic_eligibility'
    return 'OTHER','其他资格要求','other:'+sha256(text.encode()).hexdigest()[:12]


def semantic(text):
    text=PREFIX.sub('',text)
    text=re.sub(r'\s+|[，,。；;：:]','',text)
    return text.replace('应','须')


def extract(doc,clauses):
    if not clauses:
        return dict(raw_text='',raw_text_evidence=[],items=[],source_items=[],issues=['未定位公告资格条款'])
    parent=doc.announcement.capture(clauses[0]['start'],clauses[-1]['end'])
    sources=[]
    def add(value,capture,role):
        value=PREFIX.sub('',value)
        typ,name,identity=classify(value)
        level_match=re.search(r'([壹贰叁一二三甲乙丙特]级(?:及以上)?)',value)
        qname_match=re.search(r'([\u4e00-\u9fff]{2,16}(?:施工总承包|专业承包))',value)
        sources.append(dict(type=typ,name=name,requirement=value,page=capture['page'],evidence=capture['evidence'],
                            identity=identity,level=level_match[1] if level_match and typ in {'PROFESSIONAL_QUALIFICATION','PERSONNEL'} else None,
                            qualification_name=qname_match[1] if qname_match else '注册建造师' if typ=='PERSONNEL' and '注册建造师' in value else None,
                            source_role=role))
    for clause in clauses:
        capture=doc.announcement.capture(clause['start'],clause['end'],'公告资格条款','同级资格条款边界')
        add(clause['text'],capture,'ANNOUNCEMENT')
    front=doc.front_content
    start=next((front.text.find(x) for x in ['（1）资质要求','(1)资质要求'] if front.text.find(x)>=0),-1)
    end=front.text.find('☑不接受',start)
    if start>=0 and end>start:
        block=front.text[start:end]
        markers=list(re.finditer(r'[（(](?P<num>\d+)[）)]',block))
        for i,m in enumerate(markers):
            stop=markers[i+1].start() if i+1<len(markers) else len(block)
            a=start+m.end();b=start+stop
            capture=front.capture(a,b,'前附表资格条件','编号条款边界')
            add(block[m.end():stop],capture,'FRONT_TABLE')
    # Some PDFs encode the end of the financial value in the merged left-cell text
    # item. Recover only the explicit missing suffix and keep its own coordinates.
    from .document import View
    for item in sources:
        if item['source_role']!='FRONT_TABLE' or item['type']!='FINANCIAL' or '财务审计报告' in item['requirement']:continue
        candidates=[n for n in doc.nodes if n['page'] in item['page'] and '财务审计报告' in n['text']]
        if len(candidates)==1:
            extra=View(candidates).find(r'(?P<v>财务审计报告[；;]?)','前附表合并单元格尾部','原文坐标补全')[0]
            item['requirement']+=extra['value'];item['page']=sorted(set(item['page']+extra['page']));item['evidence']+=extra['evidence']
    for i,item in enumerate(sources,1):item['source_item_id']=f'qualification_source_{i:03d}'
    grouped={}
    for item in sources:grouped.setdefault(item['identity'],[]).append(item)
    items=[]
    for identity,members in grouped.items():
        selected=max(members,key=lambda x:len(semantic(x['requirement'])))
        sem=[semantic(x['requirement']) for x in members]
        if len(members)==1:merge='NONE'
        elif len(set(sem))==1:merge='EXACT_DUPLICATE'
        elif any(a in b for a in sem for b in sem if a!=b):merge='SUPPLEMENT'
        else:merge='SEMANTIC_DUPLICATE'
        evidences=[{**e,'source_item_id':m['source_item_id'],'source_role':m['source_role'],
                    'source_requirement':m['requirement']} for m in members for e in m['evidence']]
        cid='qual_'+sha256(identity.encode()).hexdigest()[:16]
        levels={m['level'] for m in members if m['level']}
        items.append(dict(canonical_item_id=cid,type=selected['type'],name=selected['name'],requirement=selected['requirement'],
                          requirement_parts=[selected['requirement']],raw_text='\n'.join(e['text'] for e in evidences),
                          evidences=evidences,page=sorted({e['page'] for e in evidences}),merge_type=merge,
                          qualification_name=selected['qualification_name'],level=next(iter(levels)) if len(levels)==1 else None,
                          status='CONFLICT' if len(levels)>1 else 'REVIEW_REQUIRED',
                          note='canonical requirement直接采用信息最完整的来源原文，全部重复或补充来源保存在evidences。',
                          review={'status':'UNREVIEWED','value':None,'note':''}))
    return dict(raw_text=''.join(c['text'] for c in clauses),raw_text_evidence=parent['evidence'],items=items,source_items=sources,
                issues=['资格拆分、分类及语义合并为机器结果，需与人工金标准回归。'])
