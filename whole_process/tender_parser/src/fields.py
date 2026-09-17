"""Label-specific extraction. All candidates retain their own source evidence."""
import re
from datetime import datetime
from .document import clean

RENAMES={'项目编号/招标编号':'招标编号','获取方式':'招标文件获取方式'}
EXTENSIONS=['预算金额','最高投标限价','投标保证金','招标项目编号','交货地点','招标文件获取时间','资格要求结构化项']
DATE=r'\d{4}(?:年|[-/.])\d{1,2}(?:月|[-/.])\d{1,2}(?:日)?(?:\d{1,2}[时:]\d{1,2}(?:分)?)'
MONEY=r'(?:\d[\d,]*(?:\.\d+)?(?:亿元|万元|元)|[零壹贰叁肆伍陆柒捌玖拾佰仟万亿元整]+[（(]\d[\d,]*(?:\.\d+)?元[）)])'
MAJOR_SECTION=r'(?:[一二三四五六七八九十]+)[、.．](?:招标|投标|开标|资格|发布|其他|监督|联系)'


def date_value(value):
    # Compact date loses the space between day and hour: keep source text spacing for parsing.
    text='\n'.join(e['text'] for e in value['evidence'])
    text=re.sub(r'\s+',' ',text).strip()
    matches=list(re.finditer(r'(\d{4})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*(?:日|\s+)\s*(\d{1,2})\s*[时:]\s*(\d{1,2})',text))
    if not matches:
        dates=list(re.finditer(r'(\d{4})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*(?:日)?',text))
        if not dates:return False
        try:
            parts=[tuple(map(int,m.groups())) for m in dates]
            [datetime(y,mo,day) for y,mo,day in parts]
        except ValueError:return False
        value['raw_value']=value['value']
        value['value']='至'.join(f'{y}年{mo}月{day}日' for y,mo,day in parts)
        return True
    try:
        values=[tuple(map(int,m.groups())) for m in matches]
        [datetime(*parts) for parts in values]
    except ValueError:return False
    dates=[f'{y}年{mo}月{day}日{hour}时{minute:02d}分' for y,mo,day,hour,minute in values]
    value['raw_value']=value['value'];value['value']='至'.join(dates);return True


def key(value):return clean(value).rstrip('。；;')


LEGAL_ENTITY = re.compile(
    r'(?P<v>[\u4e00-\u9fffA-Za-z0-9（）()·]+?'
    r'(?:有限责任公司|股份有限公司|有限公司)(?:[\u4e00-\u9fff]{1,20}分公司)?|'
    r'[\u4e00-\u9fffA-Za-z0-9（）()·]+?(?:管理委员会|委员会|事业服务中心|服务中心|发展中心|教育局|管理局|学院|学校))')


def normalize_entity_candidates(candidates):
    """Keep the named legal entity out of addresses, page marks and later contact text."""
    for candidate in candidates:
        value=re.sub(r'^[-—－]+\d+[-—－]+','',candidate['value'])
        if '名称：' in value:value=value.split('名称：',1)[1]
        match=LEGAL_ENTITY.search(value)
        if match:
            candidate['raw_value']=candidate['value']
            candidate['value']=match['v']


def truncate_candidate(candidate,end):
    """Trim normalized value and its evidence to the same character boundary."""
    candidate['raw_value']=candidate['value']
    candidate['value']=candidate['value'][:end]
    remaining=end;evidence=[]
    for source in candidate.get('evidence',[]):
        if remaining<=0:break
        cut=source.copy();count=0;stop=0
        for stop,char in enumerate(source['text'],1):
            if not char.isspace():count+=1
            if count>=remaining:break
        cut['end']=cut['start']+stop;cut['text']=cut['text'][:stop]
        evidence.append(cut);remaining-=count
    candidate['evidence']=evidence
    candidate['page']=sorted({e['page'] for e in evidence})


def finalize(name,candidates,note=''):
    preserve_terminal=name in {'项目规模','申请人资格要求/投标人资格要求','招标文件获取方式','递交方法'}
    valid=[]
    for c in candidates:
        boundary=re.search(MAJOR_SECTION,c['value'])
        if boundary:truncate_candidate(c,boundary.start())
        if not c['value'] or re.search(r'^(?:见|详见|同投标截止时间)',c['value']):continue
        if not preserve_terminal:c['value']=c['value'].rstrip('。；;')
        valid.append(c)
    if name in {'招标编号','招标项目编号'} and len(valid)>1:
        longest=max(valid,key=lambda c:len(key(c['value'])))
        if all(key(c['value']) in key(longest['value']) for c in valid):valid=[longest]
    if name in {'招标人/采购人名称','招标代理机构'} and len(valid)>1:
        legal=[c for c in valid if re.search(r'(?:公司|中心|局|委员会|学院|学校|政府)$',key(c['value']))]
        if legal:
            shortest=min(legal,key=lambda c:len(key(c['value'])))
            if all(key(shortest['value']) in key(c['value']) or key(c['value'])==key(shortest['value']) for c in valid):valid=[shortest]
    values=list(dict.fromkeys(key(c['value']) for c in valid))
    return dict(field=name,value=valid[0]['value'] if len(values)==1 else None,
                page=sorted({p for c in valid for p in c['page']}),evidence=[e for c in valid for e in c['evidence']],candidates=valid,
                status='EXTRACTED' if len(values)==1 else 'CONFLICT' if values else 'MISSING',
                note=note,review={'status':'UNREVIEWED','value':None,'note':''})


def extract_fields(doc,definitions):
    names=list(dict.fromkeys(RENAMES.get(x['field_name'],x['field_name']) for x in definitions['fields']))+EXTENSIONS
    pools={name:[] for name in names};notes={};unmapped=[]
    def add(name,values):pools[name]+=values
    def find(name,view,pattern,label):add(name,view.find(pattern,label,'明确标签及限定边界'))
    def find_if_empty(name,view,pattern,label):
        if not pools[name]:find(name,view,pattern,label)
    ann,front=doc.announcement,doc.front_content
    for name,labels in [('项目名称',['项目名称','招标项目名称']),('项目规模',['项目规模','项目概况','建设内容']),
                        ('项目地点',['项目地点','建设地点']),('工期/服务期/供货日期',['建设工期','工期','服务期','供货日期','交货期']),
                        ('质量要求',['质量要求']),('招标内容与范围',['招标范围','招标内容与范围'])]:
        add(name,doc.clause_fields(labels,'公告同级条款边界'))
    # Opening method is a short enum-like value.  A major Chinese-numbered
    # heading may share the same machine clause when the source omits Arabic
    # subsection numbers, so never retain the rest of that clause.
    for view in [ann,front]:
        if not pools['项目总投资/估算金额']:
            find('项目总投资/估算金额',view,r'项目总投资(?:为)?[：:]?(?P<v>'+MONEY+r')','项目总投资')
        find('开启方式',view,
             r'(?:开标方式|开启方式|开标形式)[：:]?(?:本次开标为)?(?P<v>远程线上开标|线上开标|网上开标|线下开标|现场开标|不见面开标|远程开标|在线开标)',
             '明确开标方式名称')
    if not pools['开启方式']:
        for view in [ann,front]:
            find('开启方式',view,
                 r'(?:开标方式|开启方式|文件开启方式|开标地点)[：:]?.{0,100}?(?P<v>远程线上开标|线上开标|网上开标|线下开标|现场开标|不见面开标|远程开标|在线开标)',
                 '开标标签内的明确方式名称')
    for c in pools['招标内容与范围']:
        m=re.search(r'(?:\d+)?不分标段[：:](?P<v>.+)',c['value'])
        if m:c['raw_value']=c['value'];c['value']=m['v']
    for name,label in [('项目名称','项目名称'),('项目名称','招标项目名称'),('项目地点','建设地点'),
                       ('资金来源','资金来源'),('资金来源','资金来源及比例')]:
        # Funding-source cells are commonly wrapped above and below the
        # vertically centred label.  Prefer the full coordinate-bounded cell.
        if name=='资金来源':
            row=doc.row_values(label);band=doc.row_band_values(label)
            # Use the wrapped cell only when it demonstrably extends the
            # same-baseline value; otherwise retain the established row rule.
            row_value=key(row[0]['value']) if row else ''
            band_value=key(band[0]['value']) if band else ''
            values=band if band and (not row or (row_value in band_value and not band_value.endswith(row_value))) else row
        else:
            values=doc.row_values(label)
        if name=='资金来源':
            for c in values:
                c['raw_value']=c['value'];c['value']=re.sub(r'^由','',c['value'])
        add(name,values)
    find('招标编号',ann,r'(?<!项目)招标编号[：:](?P<v>[A-Za-z0-9][A-Za-z0-9_-]+)','招标编号')
    find('招标编号',doc.cover,r'(?<!项目)招标编号[：:](?P<v>[A-Za-z0-9][A-Za-z0-9_-]+)','封面招标编号')
    full_tender_number=r'(?P<v>(?:[A-Za-z0-9_-]+招[【\[(（]?\d{4}[】\])）]?\d+号?|[\u4e00-\u9fff]{1,10}招字[（(]\d{4}[）)]第\d+号))'
    find('招标编号',ann,r'(?<!项目)招标编号[：:]?'+full_tender_number,'完整格式招标编号')
    find('招标编号',doc.cover,r'(?<!项目)招标编号[：:]?'+full_tender_number,'封面完整格式招标编号')
    find_if_empty('招标编号',doc.cover,r'采购编号[：:]?(?P<v>[A-Za-z0-9][A-Za-z0-9_.\/-]+)','采购文件编号')
    find_if_empty('招标编号',ann,r'采购编号[：:]?(?P<v>[A-Za-z0-9][A-Za-z0-9_.\/-]+)','采购公告编号')
    find('招标项目编号',ann,r'招标项目编号[：:](?P<v>[A-Za-z0-9][A-Za-z0-9_-]+)','招标项目编号')
    find('招标项目编号',doc.cover,r'招标项目编号[：:]?(?P<v>[A-Za-z0-9][A-Za-z0-9_-]+)','封面招标项目编号')
    procurement_id=r'(?<!招标)项目编号[：:]?(?P<v>[A-Za-z0-9][A-Za-z0-9_.\/-]+?)(?=采购人|招标人|代理机构|[一二三四五六七八九十]+[、.]|\d+[、.](?:采购方式|招标范围|项目内容)|$)'
    find_if_empty('招标项目编号',doc.cover,procurement_id,'采购项目编号')
    find_if_empty('招标项目编号',ann,procurement_id,'采购项目编号')
    unmapped=doc.clause_fields(['项目编号'],'编号标签类型待确认，不代替招标编号')
    if unmapped:notes['招标编号']='文中另有“项目编号”，已保存在未映射编号中；不能自动视为招标编号。封面图片未OCR。'
    # Government procurement and negotiation documents use major Chinese
    # sections and simple numbered labels rather than 1.1-style clauses.
    simple_prefix=r'(?:\d+[、.]?)?'
    find_if_empty('项目名称',ann,simple_prefix+r'(?:采购)?项目名称[：:]?(?P<v>.+?)(?='+simple_prefix+r'(?:项目编号|采购编号|采购人|本框架协议的采购人|采购方式|招标范围|项目内容)[：:]?|$)','采购公告项目名称')
    find_if_empty('项目名称',doc.cover,r'(?P<v>.+?项目)(?=(?:谈判|磋商|单一来源|框架协议)?采购文件(?:采购编号|项目编号))','采购文件封面项目名称')
    find_if_empty('项目名称',ann,r'本招标项目(?P<v>.+?)(?:项目)?[（(]招标项目编号[：:]','招标条件项目名称')
    find_if_empty('项目地点',ann,r'(?:履行合同的地域范围|服务地点|项目地址|建设地点)[：:]?(?P<v>.+?)(?=(?:\d+[、.]?)?(?:供应商资格|服务期限|服务期|供货期限|质量要求)|[一二三四五六七八九十]+[、.]|$)','履约或服务地点')
    find_if_empty('项目地点',ann,r'招标项目所在地区[：:]?(?P<v>[\u4e00-\u9fff.、，]{2,45}?)(?=招标人|采购人|招标代理|采购代理|项目概况|\d+[.、]招标条件|$)','招标项目所在行政地区')
    find_if_empty('交货地点',ann,r'(?:供货地点|交货地点)[：:]?(?P<v>.+?)(?=(?:\d+[、.]?)?(?:供货期限|交货期|质保期|供应商资格)|[三四五六七八九十]+[、.]|$)','采购供货地点')
    duration_pattern=r'(?:合同履行期限[：:]?(?:服务期)?|服务期限|服务期|供货期限|供货期)[：:]?(?P<v>(?:(?:自合同签订|签订协议)之日起)?(?:\d+|[一二三四五六七八九十两]+)(?:日历天|天|个月|年)(?:[，,]质保期\d+年)?)'
    find_if_empty('工期/服务期/供货日期',ann,duration_pattern,'采购履约期限')
    find_if_empty('招标内容与范围',ann,r'\d+[、.]采购范围[：:]?(?P<v>.+?)(?=(?:\d+[、.]?)?(?:供货地点|服务地点|供货期限|服务期限|质量要求)|[一二三四五六七八九十]+[、.]|$)','编号采购范围')
    scope_pattern=r'(?:招标范围|采购范围(?!及相关要求))[：:]?(?P<v>.+?)(?=(?:\d+[、.]?)?(?:供货地点|服务地点|供货期限|服务期限|质量要求)|[一二三四五六七八九十]+[、.]|$)'
    find_if_empty('招标内容与范围',ann,scope_pattern,'采购或招标范围')
    if not pools['项目规模']:
        find('项目规模',ann,r'采购需求[：:]?(?P<v>.+?)(?=(?:\d+[、.]?)?(?:合同履行期限|服务期限|适用本框架协议)|[二三四五六七八九十]+[、.]|$)','采购需求原文')
    if not pools['招标内容与范围'] and pools['项目规模']:
        for candidate in pools['项目规模']:
            if candidate.get('label') == '采购需求原文':
                copied=dict(candidate);copied['rule']='采购文件以“采购需求”明示采购内容与范围'
                add('招标内容与范围',[copied])
    find('资金来源',ann,r'资金来源(?:为|[:：])?(?P<v>.+?)(?=[，,]招标人(?:为|[：:]))','资金来源')
    # Human-approved contextual classifications remain explicit rule outputs and
    # retain the source sentence that supports the mapping.
    for c in doc.general.find(r'(?P<v>根据《.+?本招标项目已具备招标条件.+?公开招标)','依法招标上下文','人工确认的上下文分类规则'):
        c['raw_value']=c['value'];c['value']='依法';add('项目性质',[c])
    for c in front.find(r'同类型业绩指[：:]?(?P<v>[\u4e00-\u9fff]+?)(?=相关业绩|业绩)','同类型业绩定义','人工确认的行业分类规则'):
        add('所属行业',[dict(c)]);add('项目类型/行业分类',[dict(c)])
    proxy=ann.find(r'(?P<v>(?:招标代理机构|采购代理机构|代理机构|招标代理)[：:].+?)(?=地址[：:]|联系人[：:]|$)','代理机构明示','人工确认的组织形式规则')
    if proxy:
        c=proxy[0];c['raw_value']=c['value'];c['value']='委托招标';add('组织形式',[c])
    # Keep an explicit delivery location first.  For construction tenders only,
    # an explicit 建设地点 is the operational delivery/performance location too.
    add('交货地点',doc.clause_fields(['交货地点'],'独立交货地点标签')+doc.row_values('交货地点'))
    if not pools['交货地点'] and '施工' in doc.all.text:
        construction_locations=doc.clause_fields(['建设地点'],'施工项目建设地点')+doc.row_values('建设地点')
        if not construction_locations:construction_locations=pools['项目地点'][:1]
        for c in construction_locations:
            copied=dict(c);copied['rule']='施工项目中建设地点映射为交货地点'
            add('交货地点',[copied])
    for name,label in [('项目总投资/估算金额',r'(?:项目总投资|估算金额)'),('预算金额',r'预算金额'),('招标金额',r'招标金额'),
                       ('最高投标限价',r'(?:最高投标限价(?:（招标控制价）)?(?:为)?[：:]?(?:招标控制总价[：:])?|招标控制总价[：:])'),
                       ('投标保证金',r'投标保证金(?:的)?金额(?:为)?')]:
        for view in [ann,front,doc.general]:find(name,view,label+r'[：:]?\s*(?P<v>'+MONEY+r')',name)
    for view in [ann,front,doc.general]:
        if not pools['最高投标限价']:
            find('最高投标限价',view,r'最高限价[^。；;¥￥]{0,45}[¥￥][：:]?(?P<v>\d[\d,]*(?:\.\d+)?(?:万元|元))','采购最高限价')
            find('最高投标限价',view,r'最高投标限价总价[：:]?(?P<v>\d[\d,]*(?:\.\d+)?(?:万元|元))','最高投标限价总价')
        if not pools['投标保证金']:
            find('投标保证金',view,r'(?:响应保证金[：:]?[¥￥]?|保证金金额[：:]?)(?P<v>(?:人民币)?[零壹贰叁肆伍陆柒捌玖拾佰仟万亿元整]+|\d[\d,]*(?:\.\d+)?元(?:（人民币[零壹贰叁肆伍陆柒捌玖拾佰仟万亿元整]+）)?)','响应保证金金额')
        find('投标保证金',view,r'本项目(?P<v>不收取)投标保证金','明确不收取投标保证金')
    # Front tables in construction documents often use explicit content labels
    # inside a merged cell rather than putting the value beside the row label.
    if not pools['工期/服务期/供货日期']:
        find('工期/服务期/供货日期',front,r'计划工期[：:](?P<v>\d+(?:日历天|天|个月|月))','计划工期')
    if not pools['质量要求']:
        quality=[]
        for pattern,label in [
            (r'(?P<v>工程交工验收的质量评定[：:](?:合格|优良))','交工验收质量'),
            (r'(?P<v>竣工验收的质量评定[：:](?:合格|优良))','竣工验收质量'),
        ]:
            quality.extend(front.find(pattern,label,'质量要求合并单元格内的明确分项'))
        if quality:
            add('质量要求',[dict(
                value='；'.join(c['value'] for c in quality),
                page=sorted({p for c in quality for p in c['page']}),
                evidence=[e for c in quality for e in c['evidence']],
                label='质量要求',rule='合并交工与竣工两项原文要求',parts=quality,
            )])
    if not pools['质量要求']:
        for view in [ann,front]:
            find_if_empty('质量要求',view,r'质量要求[：:]?(?P<v>合格|优良|符合[^。；;]{1,80}?(?:标准|要求))','明确质量要求')
    if not pools['质量要求']:
        for candidate in doc.row_band_values('质量要求'):
            stop=re.search(r'详细情况见|(?:\d+|[一二三四五六七八九十]+)[、.．](?:申请人|投标人)资格',candidate['value'])
            if stop:truncate_candidate(candidate,stop.start())
            if candidate['value'] in {'合格','优良'}:add('质量要求',[candidate])
    if not pools['招标内容与范围']:
        for label in ['招标范围','招标内容与范围']:
            for candidate in doc.row_band_values(label):
                stop=re.search(r'详细情况见|(?:\d+|[一二三四五六七八九十]+)[、.．](?:申请人|投标人)资格',candidate['value'])
                if stop:truncate_candidate(candidate,stop.start())
                if candidate['value']:add('招标内容与范围',[candidate])
    # The main guarantee amount is the value immediately following the exact
    # amount label.  Later credit-grade reductions are separate conditions.
    find('投标保证金',front,
         r'投标保证金的金额[：:]?(?P<v>人民币[零壹贰叁肆伍陆柒捌玖拾佰仟万亿元整]+[（(][¥￥]?\d[\d,]*(?:\.\d+)?元[）)])',
         '投标保证金主金额')
    # Qualification admission paragraphs share the clause-number prefix with the main requirement.
    first=next((c for c in doc.clauses if re.search(r'本次招标要求投标人|投标人应.*独立法人',c['text'])),None)
    qclauses=[]
    if first:
        prefix=first['number'].split('.')[0]
        qclauses=[c for c in doc.clauses if c['number'].split('.')[0]==prefix]
        # Chinese-numbered major headings sometimes occur inside the final
        # Arabic-numbered machine clause.  Trim at the first next-section
        # heading so both the raw qualification field and structured items use
        # the same admission-only boundary.
        heading=re.compile(r'(?:[一二三四五六七八九十]+)[、.．](?:招标文件(?:的)?获取|获取招标文件|投标文件(?:的)?递交|开标|发布公告|其他公告内容|监督部门|联系方式)')
        bounded=[]
        for clause in qclauses:
            match=heading.search(clause['text'])
            if match:
                if match.start()>0:
                    trimmed=dict(clause,text=clause['text'][:match.start()],end=clause['start']+match.start())
                    bounded.append(trimmed)
                break
            bounded.append(clause)
        qclauses=bounded
        c=ann.capture(qclauses[0]['start'],qclauses[-1]['end'],'公告投标人资格要求','同级资格条款组，含跨页')
        c['value']=''.join(clause['text'] for clause in qclauses)
        add('申请人资格要求/投标人资格要求',[c])
    else:
        procurement_q=re.search(
            r'(?:餐饮劳务服务企业资格要求|供应商应具备的资格条件|供应商资格要求|申请人资格要求)[：:]?(?P<v>.+?)(?=(?:\d+|[一二三四五六七八九十]+)[、.．](?:资格预审|获取|招标文件|采购文件|谈判采购文件|申请文件|响应文件|开标|发布)|$)',
            ann.text)
        if procurement_q:
            start=procurement_q.start('v');end=procurement_q.end('v')
            c=ann.capture(start,end,'采购公告供应商资格要求','采购资格章节边界')
            add('申请人资格要求/投标人资格要求',[c])
            qclauses=[{'number':'procurement','start':start,'end':end,'text':c['value']}]
        elif not pools['申请人资格要求/投标人资格要求']:
            full_front=re.search(r'(?P<v>[（(]1[）)]资质要求[：:].+?)(?=☑不组织|□不组织|踏勘现场)',front.text)
            if full_front:
                add('申请人资格要求/投标人资格要求',[
                    front.capture(full_front.start('v'),full_front.end('v'),'前附表资格要求','资格行至下一业务行边界')])
        if not pools['申请人资格要求/投标人资格要求']:
            for label in ['投标人资质、能力和信誉','投标人资格要求','申请人资格要求']:
                candidates=doc.row_band_values(label)
                if candidates:
                    candidate=candidates[0]
                    stop=re.search(r'详细情况见第七章|(?:\d+|[一二三四五六七八九十]+)[、.．](?:招标文件|投标文件|开标)',candidate['value'])
                    if stop:truncate_candidate(candidate,stop.start())
                    add('申请人资格要求/投标人资格要求',[candidate])
                    break
    # Acquisition block must explicitly mention which file is obtained.
    acquisition=next((c for c in doc.clauses if re.match(r'(?:招标文件|资格预审文件|预审文件)?获取方式[:：]',c['text'])),None)
    if acquisition:
        kind='招标文件' if '招标文件' in acquisition['text'] and '预审文件' not in acquisition['text'] else None
        if kind:
            stop=re.search(r'注[：:]',acquisition['text']);end=stop.start() if stop else len(acquisition['text'])
            begin=re.match(r'(?:招标文件|资格预审文件|预审文件)?获取方式[:：]',acquisition['text']).end()
            add(kind+'获取方式',[ann.capture(acquisition['start']+begin,acquisition['start']+end,kind+'获取方式','限定所获文件种类')])
            time_clause=next((c for c in doc.clauses if c['number'].split('.')[0]==acquisition['number'].split('.')[0] and c['text'].startswith('获取时间')),None)
            if time_clause:
                m=re.search(r'获取时间[:：](?P<v>.+?)(?=[（(]|$)',time_clause['text'])
                if m:
                    c=ann.capture(time_clause['start']+m.start('v'),time_clause['start']+m.end('v'),kind+'获取时间','日期范围与文件种类')
                    if date_value(c):add(kind+'获取时间',[c])
    procurement_date=r'\d{4}年\d{1,2}月\d{1,2}日(?:\d{1,2}(?:时|:)\d{1,2}(?:分)?)?'
    if not pools['招标文件获取时间']:
        for c in ann.find(r'获取时间[：:]?(?P<v>'+procurement_date+r'至'+procurement_date+r')','采购文件获取时间','获取时间明确日期范围'):
            if date_value(c):add('招标文件获取时间',[c])
    if not pools['招标文件获取方式']:
        find('招标文件获取方式',ann,
             r'获取(?:方式|方法)[：:]?(?P<v>.+?)(?=(?:\d+[、.]?)?(?:售价|响应文件|递交截止|递交时间)|[四五六七八九十]+[、.]|$)',
             '采购文件获取方式')
    # Post-prequalification invitation letters often describe acquisition and
    # submission in prose instead of numbered announcement clauses.
    if not pools['招标文件获取时间']:
        for c in ann.find(
            r'请你单位于(?P<v>\d{4}年\d{1,2}月\d{1,2}日\d{1,2}时\d{1,2}分至\d{4}年\d{1,2}月\d{1,2}日\d{1,2}时\d{1,2}分)',
            '邀请书招标文件获取时间','邀请书购买下载招标文件句'):
            if date_value(c):add('招标文件获取时间',[c])
    if not pools['招标文件获取方式']:
        find('招标文件获取方式',ann,
             r'(?P<v>通过互联网使用CA数字证书登录.+?购买下载招标文件)',
             '邀请书招标文件获取方式')
    if not pools['招标文件获取方式']:
        find('招标文件获取方式',ann,
             r'(?P<v>在[^。；;]{1,180}?(?:电子招投标交易平台|电子交易平台)[^。；;]{0,80}?购买招标文件)',
             '邀请书平台购买招标文件')
    if not pools['递交截止时间']:
        for c in ann.find(
            r'(?:投标文件递交的|递交投标文件的)截止时间(?:（投标截止时间，下同）)?为(?P<v>\d{4}年\d{1,2}月\d{1,2}日\d{1,2}时\d{1,2}分)',
             '邀请书投标截止时间','投标文件递交截止句'):
            if date_value(c):add('递交截止时间',[c])
    if not pools['递交截止时间']:
        for candidate in doc.row_values('投标截止时间')+doc.row_values('递交截止时间'):
            if date_value(candidate):add('递交截止时间',[candidate])
    for name,labels in [('开标时间',['开标时间']),('开启时间',['开标时间','开启时间']),('递交截止时间',['投标文件递交截止时间','递交截止时间'])]:
        for c in doc.clause_fields(labels,'明确时间标签'):
            # Date takes only prefix; later late-submission clauses are not part of the value.
            text='\n'.join(e['text'] for e in c['evidence'])
            m=re.search(r'\d{4}\s*[年/-].*?\d{1,2}\s*[时:]\s*\d{1,2}(?:分)?',text,re.S)
            if m:
                target=clean(m[0]);length=len(target)
                remaining=length;es=[]
                for e in c['evidence']:
                    if remaining<=0:break
                    cut=e.copy();nonspace=0;stop=0
                    for stop,ch in enumerate(e['text'],1):
                        if not ch.isspace():nonspace+=1
                        if nonspace>=remaining:break
                    cut['end']=cut['start']+stop;cut['text']=cut['text'][:stop];es.append(cut);remaining-=nonspace
                c['evidence']=es;c['value']=target;c['page']=sorted({e['page'] for e in es})
                if date_value(c):add(name,[c])
    for c in doc.clauses:
        if re.match(r'投标文件加密要求[，,：:]',c['text']):
            begin=re.match(r'投标文件加密要求[，,：:]',c['text']).end()
            add('递交方法',[ann.capture(c['start']+begin,c['end'],'投标文件加密要求','同级条款边界')])
        m=re.match(r'递交方法[：:](?P<v>.+)',c['text'])
        if m:add('递交方法',[ann.capture(c['start']+m.start('v'),c['start']+m.end('v'),'递交方法','公告同级条款边界')])
    if not pools['递交方法']:
        find('递交方法',ann,
             r'投标人应在投标截止时间前[，,](?P<v>通过互联网使用CA数字证书登录.+?将加密的投标文件上传至交易平台)',
             '邀请书电子递交方法')
    if not pools['递交方法']:
        find('递交方法',ann,r'方式为(?P<v>网上递交|在线递交|现场递交)','邀请书明确递交方式')
    if not pools['递交截止时间']:
        for c in ann.find(
            r'(?:响应文件(?:提交|递交)?的?截止时间|递交截止时间|递交时间)[：:]?(?P<v>\d{4}年\d{1,2}月\d{1,2}日\d{1,2}(?:时|:)\d{1,2}(?:分)?)',
            '采购响应文件截止时间','明确截止时间标签'):
            if date_value(c):add('递交截止时间',[c])
    if not pools['递交方法']:
        find('递交方法',ann,
             r'递交方法[：:]?(?P<v>.+?)(?=(?:\d+[、.]?)?(?:递交地址|递交地点)|[五六七八九十]+[、.]|$)',
             '采购响应文件递交方法')
    if not pools['开标时间'] or not pools['开启时间']:
        opening=front.find(
            r'投标文件第一个信封(?:（商务及技术文件）)?开标时间[：:](?P<v>\d{4}年\d{1,2}月\d{1,2}日\d{1,2}时\d{1,2}分)',
            '第一个信封开标时间','前附表明确开标时间')
        for c in opening:
            if date_value(c):
                if not pools['开标时间']:add('开标时间',[dict(c)])
                if not pools['开启时间']:add('开启时间',[dict(c)])
    if not pools['开标时间'] or not pools['开启时间']:
        opening=ann.find(
            r'(?:开标时间|开启时间|文件开启时间)[：:]?(?P<v>\d{4}(?:年|[-/.])\d{1,2}(?:月|[-/.])\d{1,2}(?:日)?\d{1,2}(?:时|:)\d{1,2}(?:分)?)',
            '采购公告开启时间','明确开标或开启时间标签')
        for c in opening:
            if date_value(c):
                if not pools['开标时间']:add('开标时间',[dict(c)])
                if not pools['开启时间']:add('开启时间',[dict(c)])
    opening_place_stop=r'(?=(?:投标文件)?第二个信封|开标形式[：:]|开标方式[：:]|开启方式[：:]|谈判小组|评审小组|[（(]1[）)]|1[、.]开标时|5\.1开标时间和地点|开标程序|评标委员会|$)'
    find('开启地点',ann,r'(?:开标地点|开启地点)[：:]?(?P<v>[^。；;]{1,120}?线上开标)','采购公告线上开标地点')
    if not pools['开启地点']:
        find('开启地点',ann,r'(?:开标地点|开启地点)[：:]?(?P<v>.+?)(?=开标方式[：:]|开启方式[：:]|谈判小组|评审小组|[七八九十]+[、.]|$)','采购公告开标地点')
    if not pools['开启地点']:
        find('开启地点',front,r'投标文件第一个信封(?:（商务及技术文件）)?开标地点[：:](?P<v>.+?)'+opening_place_stop,'第一个信封开标地点')
    if not pools['开启地点']:
        find('开启地点',front,r'开标地点[：:](?P<v>.+?)'+opening_place_stop,'开标地点')
    for c in pools['开启地点']:
        c['value']=re.sub(r'https?://[^\s）)]+/?$','',c['value']).rstrip('（(')
    if pools['开启地点']:notes['开启地点']='按原文明示“开标地点”保留。若值为线上开标，表示虚拟方式，不推断实体地址。'
    for view in [doc.evaluation,front,ann]:
        find('评审办法',view,r'(?:本次评标采用|采用的评标方法)(?P<v>综合评估法|综合评分法|合理低价法|经评审的最低投标价法|最低评标价法)','明确采用的评标方法')
        find('评审办法',view,r'第三章评标办法[（(](?P<v>综合评估法|综合评分法|合理低价法|经评审的最低投标价法|最低评标价法)[）)]','评标办法章标题')
        find('评审办法',view,r'(?:入围评审方法|评审办法|评审方法)[：:]?(?P<v>综合评估法|综合评分法|合理低价法|合格制|价格优先法|质量优先法|最低价法)','采购评审方法')
        find('评审办法',view,r'(?:入围评审方法|评审办法|评审方法).{0,120}?(?P<v>综合评估法|综合评分法|合理低价法|合格制|最低价法)','评审表格方法名称')
        find('评审办法',view,r'☑(?P<v>综合评估法|综合评分法|合理低价法|经评审的最低投标价法|最低评标价法)','勾选评审方法')
    # Distinct contact blocks. Supervision contacts can never enter owner/agent fields.
    markers=list(re.finditer(r'(?P<role>招标人|采购人|招标代理机构|采购代理机构|招标代理|代理机构)(?:信息)?[：:]',ann.text))
    for i,m in enumerate(markers):
        a=m.end();b=markers[i+1].start() if i+1<len(markers) else len(ann.text)
        text=ann.text[a:b];owner=m['role'] in {'招标人','采购人'};prefix='招标人' if owner else '招标代理机构'
        if not owner and not pools['组织形式']:
            organization=ann.capture(m.start(),m.end(),m['role'],'代理机构明示')
            organization['raw_value']=organization['value'];organization['value']='委托招标'
            add('组织形式',[organization])
        patterns=[('招标人/采购人名称' if owner else '招标代理机构',r'^(?:名称[：:])?(?P<v>.+?)(?=(?:详细)?地址[:：]|联系人[:：]|$)'),
                  (prefix+'地址',r'(?:详细)?地址[:：](?P<v>.+?)(?=联系人[:：]|电话[:：]|联系方式[:：]|联系电话[:：]|$)'),
                  (prefix+'联系人',r'联系人[:：](?P<v>.+?)(?=电话[:：]|联系方式[:：]|联系电话[:：]|电子邮箱[:：]|$)'),
                  (prefix+'联系方式',r'(?:电话|联系方式|联系电话)[:：](?P<v>(?:1\d{10}|0\d{2,3}[—-]?\d{7,8})(?:[、,，](?:1\d{10}|0\d{2,3}[—-]?\d{7,8}))*)')]
        for name,pat in patterns:
            hits=list(re.finditer(pat,text))
            if name.endswith('联系方式') and hits:hits=hits[:1]
            for hit in hits:add(name,[ann.capture(a+hit.start('v'),a+hit.end('v'),m['role'],'联系人所属主体块')])
    normalize_entity_candidates(pools['招标人/采购人名称'])
    normalize_entity_candidates(pools['招标代理机构'])
    for address_field in ['招标人地址','招标代理机构地址']:
        pools[address_field]=[candidate for candidate in pools[address_field]
                              if len(candidate['value']) <= 100 and
                              not re.search(r'交易平台|注册指南|公告发布|https?://',candidate['value'])]
    find('发布网站',ann,r'本次招标公告在(?P<v>《.+?》(?:[、,，]《.+?》)*)(?=上(?:同时)?发布)','公告发布媒介')
    if not pools['发布网站']:
        for c in doc.clauses:
            if '招标公告' in c['text'] and '发布' in c['text']:
                m=re.search(r'(?:在)(?P<v>《.+?》(?:[、,，]《.+?》)*)(?=上?发布)',c['text'])
                if m:add('发布网站',[ann.capture(c['start']+m.start('v'),c['start']+m.end('v'),'公告发布媒介','发布句限定')])
    if not pools['发布网站']:
        media=re.search(r'(?:本次谈判采购公告|本项目采购公告|采购公告).{0,12}?在(?P<v>.+?)(?=上?发布)',ann.text)
        if media:
            titles=re.findall(r'《[^》]+》',media['v'])
            if titles:
                c=ann.capture(media.start('v'),media.end('v'),'采购公告发布媒介','采购公告发布句限定')
                c['raw_value']=c['value'];c['value']='、'.join(titles);add('发布网站',[c])
    if not pools['发布网站']:
        media=re.search(r'(?:公告发布媒介在|(?:本次|本项目)?(?:招标|采购)公告.{0,20}?在)(?P<v>.+?)(?=上(?:同时)?发布)',ann.text)
        if media:
            titles=re.findall(r'《[^》]+》',media['v'])
            if titles:
                c=ann.capture(media.start('v'),media.end('v'),'公告发布媒介','通用发布句限定')
                c['raw_value']=c['value'];c['value']='、'.join(titles);add('发布网站',[c])
    # Retain front-table owner address independently; full/short address differences require review.
    m=re.search(r'招标人[:：].+?地址[:：](?P<v>.+?)(?=联系人[:：])',front.text)
    if m:add('招标人地址',[front.capture(m.start('v'),m.end('v'),'前附表招标人地址','主体及联系人边界')])
    guarantee_view=front if front.text else doc.general
    find('投标保证金方式',guarantee_view,r'投标保证金的递交形式[：:]1[.、](?P<v>.+?[；;]2[.、].+?)(?=递交时限[：:]|$)','保证金并列形式')
    if pools['投标保证金方式']:
        for c in pools['投标保证金方式']:
            m=re.search(r'(?P<a>.+?)[；;]2[.、](?P<b>.+)',c['value'])
            if m:c['value']=m['a'].rstrip('；;')+'；'+m['b'].rstrip('；;')
    else:
        find('投标保证金方式',guarantee_view,r'投标保证金可采用的形式[：:](?P<v>.+?)(?=①|1[.、]|投标保证金的缴纳|$)','保证金可采用形式')
        find('投标保证金方式',guarantee_view,r'\(1\)(?P<v>电汇或转账[：:].+?)(?=单位名称[：:])','保证金支付形式1')
        find('投标保证金方式',guarantee_view,r'\(2\)(?P<v>保函.+?)(?=退还时间[：:])','保证金支付形式2')
    if not pools['投标保证金方式']:
        for view in [front,doc.general]:
            find_if_empty('投标保证金方式',view,r'(?:投标|响应)?保证金(?:的)?形式[：:]?(?P<v>.+?)(?=收款人[：:]|收款单位[：:]|\d+[、.]采用|$)','采购保证金形式')
    if not pools['投标保证金方式']:
        for view in [front,doc.general]:find_if_empty('投标保证金方式',view,r'响应保证金采用(?P<v>.+?方式之一)(?=进行交纳|[，,；;。]|$)','响应保证金形式')
    # These are explicitly enumerated parallel allowed forms, not contradictory alternatives.
    methods=pools['投标保证金方式']
    if len(methods)>1:
        pools['投标保证金方式']=[dict(value='；'.join(c['value'] for c in methods),page=sorted({p for c in methods for p in c['page']}),
            evidence=[e for c in methods for e in c['evidence']],label='原文明示并列形式',rule='有序并列原文片段',parts=methods)]
    notes['组织形式']='没有明示组织形式时不由代理机构存在自动推断。'
    notes['项目性质']='不将引用法律条文直接当作该文档明示的项目性质。'
    field_stops={
        '招标内容与范围':r'(?=(?:项目地址|建设地点|计划工期|质量要求)[：:]|(?:\d+|[一二三四五六七八九十]+)[、.．](?:申请人|投标人|供应商)?资格)',
        '质量要求':r'(?=(?:\d+|[一二三四五六七八九十]+)[、.．](?:申请人|投标人|供应商)?资格)',
        '招标文件获取方式':r'(?=(?:\d+|[一二三四五六七八九十]+)[、.．](?:申请文件|响应文件|投标文件)(?:的)?递交)',
        '递交方法':r'(?=(?:\d+|[一二三四五六七八九十]+)[、.．](?:资格预审文件开启|开标时间|开启))',
        '投标保证金方式':r'(?=退还方式|投标保证金接收账户|投标文件全部采用|\d+[、.]采用)',
    }
    for field,pattern in field_stops.items():
        for c in pools[field]:
            stop=re.search(pattern,c['value'])
            if stop:truncate_candidate(c,stop.start())
    for c in pools['招标文件获取方式']:
        c['value']=re.sub(r'(采购文件)[。.]?采购文件$' ,r'\1',c['value'])
    for c in pools['质量要求']:
        start=re.search(r'[（(]1[）)]交工验收',c['value'])
        if start and start.start()>0:c['raw_value']=c['value'];c['value']=c['value'][start.start():]
    # Remove document-template noise while retaining the source in raw_value.
    pools['项目名称']=[c for c in pools['项目名称'] if not re.search(r'开票信息|填写内容|响应文件格式|投标文件格式',c['value'])]
    for c in pools['递交方法']:
        c['value']=re.sub(r'^及地址[：:]?','',c['value'])
        upload=c['value'].find('上传加密申请文件')
        if upload>0:c['raw_value']=c['value'];c['value']=c['value'][upload:]
    for c in pools['申请人资格要求/投标人资格要求']:
        c['value']=re.sub(r'^001(?:第一标段|不分标段)?(?:申请人|投标人)?资格要求[：:]?','',c['value'])
    records=[finalize(n,pools[n],notes.get(n,'')) for n in names if n!='资格要求结构化项']
    for i,r in enumerate(records):
        r['field_id']=f'field_{names.index(r["field"])+1:03d}'
        r['group']='原字段' if names.index(r['field'])<35 else '扩展字段'
        if r['field']=='招标编号' and unmapped and r['value'] is None:r['status']='REVIEW_REQUIRED'
        if r['field']=='开启地点' and r['value'] in ['线上开标','网上开标']:r['status']='REVIEW_REQUIRED'
    return records,qclauses,unmapped
