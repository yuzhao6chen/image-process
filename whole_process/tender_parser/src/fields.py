"""Label-specific extraction. All candidates retain their own source evidence."""
import re
from datetime import datetime
from .document import clean

RENAMES={'项目编号/招标编号':'招标编号','获取方式':'招标文件获取方式'}
EXTENSIONS=['预算金额','最高投标限价','投标保证金','招标项目编号','交货地点','招标文件获取时间','预审文件获取方式','资格要求结构化项']
DATE=r'\d{4}(?:年|[-/.])\d{1,2}(?:月|[-/.])\d{1,2}(?:日)?(?:\d{1,2}[时:]\d{1,2}(?:分)?)'
MONEY=r'(?:\d[\d,]*(?:\.\d+)?(?:亿元|万元|元)|[零壹贰叁肆伍陆柒捌玖拾佰仟万亿元整]+[（(]\d[\d,]*(?:\.\d+)?元[）)])'


def date_value(value):
    # Compact date loses the space between day and hour: keep source text spacing for parsing.
    text='\n'.join(e['text'] for e in value['evidence'])
    text=re.sub(r'\s+',' ',text).strip()
    matches=list(re.finditer(r'(\d{4})\s*[年/.-]\s*(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*(?:日|\s+)\s*(\d{1,2})\s*[时:]\s*(\d{1,2})',text))
    if not matches: return False
    try:
        values=[tuple(map(int,m.groups())) for m in matches]
        [datetime(*parts) for parts in values]
    except ValueError:return False
    dates=[f'{y}年{mo}月{day}日{hour}时{minute:02d}分' for y,mo,day,hour,minute in values]
    value['raw_value']=value['value'];value['value']='至'.join(dates);return True


def key(value):return clean(value).rstrip('。；;')


def finalize(name,candidates,note=''):
    preserve_terminal=name in {'项目规模','申请人资格要求/投标人资格要求','招标文件获取方式','预审文件获取方式','递交方法'}
    valid=[]
    for c in candidates:
        if not c['value'] or re.search(r'^(?:见|详见|同投标截止时间)',c['value']):continue
        if not preserve_terminal:c['value']=c['value'].rstrip('。；;')
        valid.append(c)
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
    ann,front=doc.announcement,doc.front_content
    for name,labels in [('项目名称',['项目名称','招标项目名称']),('项目规模',['项目规模','项目概况','建设内容']),
                        ('项目地点',['项目地点','建设地点']),('工期/服务期/供货日期',['建设工期','工期','服务期','供货日期','交货期']),
                        ('质量要求',['质量要求']),('招标内容与范围',['招标范围','招标内容与范围']),
                        ('开启方式',['开标方式','开启方式'])]:
        add(name,doc.clause_fields(labels,'公告同级条款边界'))
    for c in pools['招标内容与范围']:
        m=re.search(r'(?:\d+)?不分标段[：:](?P<v>.+)',c['value'])
        if m:c['raw_value']=c['value'];c['value']=m['v']
    for name,label in [('项目名称','项目名称'),('项目名称','招标项目名称'),('项目地点','建设地点'),
                       ('资金来源','资金来源'),('资金来源','资金来源及比例')]:
        values=doc.row_values(label)
        if name=='资金来源':
            for c in values:
                c['raw_value']=c['value'];c['value']=re.sub(r'^由','',c['value'])
        add(name,values)
    find('招标编号',ann,r'(?<!项目)招标编号[：:](?P<v>[A-Za-z0-9][A-Za-z0-9_-]+)','招标编号')
    find('招标项目编号',ann,r'招标项目编号[：:](?P<v>[A-Za-z0-9][A-Za-z0-9_-]+)','招标项目编号')
    unmapped=doc.clause_fields(['项目编号'],'编号标签类型待确认，不代替招标编号')
    if unmapped:notes['招标编号']='文中另有“项目编号”，已保存在未映射编号中；不能自动视为招标编号。封面图片未OCR。'
    find('资金来源',ann,r'资金来源(?:为|[:：])?(?P<v>.+?)(?=[，,]招标人(?:为|[：:]))','资金来源')
    # Human-approved contextual classifications remain explicit rule outputs and
    # retain the source sentence that supports the mapping.
    for c in doc.general.find(r'(?P<v>根据《.+?本招标项目已具备招标条件.+?公开招标)','依法招标上下文','人工确认的上下文分类规则'):
        c['raw_value']=c['value'];c['value']='依法';add('项目性质',[c])
    for c in front.find(r'同类型业绩指[：:]?(?P<v>[\u4e00-\u9fff]+?)(?=相关业绩|业绩)','同类型业绩定义','人工确认的行业分类规则'):
        add('所属行业',[dict(c)]);add('项目类型/行业分类',[dict(c)])
    proxy=ann.find(r'(?P<v>招标代理机构[：:].+?)(?=地址[：:]|联系人[：:]|$)','代理机构明示','人工确认的组织形式规则')
    if proxy:
        c=proxy[0];c['raw_value']=c['value'];c['value']='委托招标';add('组织形式',[c])
    # Do not mix construction location with delivery location.
    add('交货地点',doc.clause_fields(['交货地点'],'独立交货地点标签')+doc.row_values('交货地点'))
    for name,label in [('项目总投资/估算金额',r'(?:项目总投资|估算金额)'),('预算金额',r'预算金额'),('招标金额',r'招标金额'),
                       ('最高投标限价',r'(?:最高投标限价(?:（招标控制价）)?(?:为)?[：:]?(?:招标控制总价[：:])?|招标控制总价[：:])'),
                       ('投标保证金',r'投标保证金(?:的)?金额(?:为)?')]:
        for view in [ann,front]:find(name,view,label+r'[：:]?\s*(?P<v>'+MONEY+r')',name)
    # Qualification admission paragraphs share the clause-number prefix with the main requirement.
    first=next((c for c in doc.clauses if re.search(r'本次招标要求投标人|投标人应.*独立法人',c['text'])),None)
    qclauses=[]
    if first:
        prefix=first['number'].split('.')[0]
        qclauses=[c for c in doc.clauses if c['number'].split('.')[0]==prefix]
        c=ann.capture(qclauses[0]['start'],qclauses[-1]['end'],'公告投标人资格要求','同级资格条款组，含跨页')
        c['value']=''.join(clause['text'] for clause in qclauses)
        add('申请人资格要求/投标人资格要求',[c])
    # Acquisition block must explicitly mention which file is obtained.
    acquisition=next((c for c in doc.clauses if re.match(r'(?:招标文件|资格预审文件|预审文件)?获取方式[:：]',c['text'])),None)
    if acquisition:
        kind='预审文件' if '预审文件' in acquisition['text'] else '招标文件' if '招标文件' in acquisition['text'] else None
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
    find('开启地点',front,r'开标地点[：:](?P<v>.+?)(?=[（(]1[）)]|1[、.]开标时|开标程序|评标委员会|$)','开标地点')
    if pools['开启地点']:notes['开启地点']='按原文明示“开标地点”保留。若值为线上开标，表示虚拟方式，不推断实体地址。'
    for view in [doc.evaluation,front]:find('评审办法',view,r'(?:本次评标采用|采用的评标方法)(?P<v>综合评估法|综合评分法|经评审的最低投标价法|最低评标价法)','明确采用的评标方法')
    # Distinct contact blocks. Supervision contacts can never enter owner/agent fields.
    markers=list(re.finditer(r'(?P<role>招标人|招标代理机构|招标代理)[：:]',ann.text))
    for i,m in enumerate(markers):
        a=m.end();b=markers[i+1].start() if i+1<len(markers) else len(ann.text)
        text=ann.text[a:b];owner=m['role']=='招标人';prefix='招标人' if owner else '招标代理机构'
        patterns=[('招标人/采购人名称' if owner else '招标代理机构',r'^(?P<v>.+?)(?=(?:详细)?地址[:：]|联系人[:：]|$)'),
                  (prefix+'地址',r'(?:详细)?地址[:：](?P<v>.+?)(?=联系人[:：]|电话[:：]|$)'),
                  (prefix+'联系人',r'联系人[:：](?P<v>.+?)(?=电话[:：]|$)'),
                  (prefix+'联系方式',r'电话[:：](?P<v>(?:1\d{10}|0\d{2,3}-?\d{7,8})(?:[、,，](?:1\d{10}|0\d{2,3}-?\d{7,8}))*)')]
        for name,pat in patterns:
            for hit in re.finditer(pat,text):add(name,[ann.capture(a+hit.start('v'),a+hit.end('v'),m['role'],'联系人所属主体块')])
    find('发布网站',ann,r'本次招标公告在(?P<v>《.+?》(?:[、,，]《.+?》)*)(?=上(?:同时)?发布)','公告发布媒介')
    # Retain front-table owner address independently; full/short address differences require review.
    m=re.search(r'招标人[:：].+?地址[:：](?P<v>.+?)(?=联系人[:：])',front.text)
    if m:add('招标人地址',[front.capture(m.start('v'),m.end('v'),'前附表招标人地址','主体及联系人边界')])
    find('投标保证金方式',front,r'投标保证金的递交形式[：:]1[.、](?P<v>.+?[；;]2[.、].+?)(?=递交时限[：:]|$)','保证金并列形式')
    if pools['投标保证金方式']:
        for c in pools['投标保证金方式']:
            m=re.search(r'(?P<a>.+?)[；;]2[.、](?P<b>.+)',c['value'])
            if m:c['value']=m['a'].rstrip('；;')+'；'+m['b'].rstrip('；;')
    else:
        find('投标保证金方式',front,r'\(1\)(?P<v>电汇或转账[：:].+?)(?=单位名称[：:])','保证金支付形式1')
        find('投标保证金方式',front,r'\(2\)(?P<v>保函.+?)(?=退还时间[：:])','保证金支付形式2')
    # These are explicitly enumerated parallel allowed forms, not contradictory alternatives.
    methods=pools['投标保证金方式']
    if len(methods)>1:
        pools['投标保证金方式']=[dict(value='；'.join(c['value'] for c in methods),page=sorted({p for c in methods for p in c['page']}),
            evidence=[e for c in methods for e in c['evidence']],label='原文明示并列形式',rule='有序并列原文片段',parts=methods)]
    notes['组织形式']='没有明示组织形式时不由代理机构存在自动推断。'
    notes['项目性质']='不将引用法律条文直接当作该文档明示的项目性质。'
    records=[finalize(n,pools[n],notes.get(n,'')) for n in names if n!='资格要求结构化项']
    for i,r in enumerate(records):
        r['field_id']=f'field_{names.index(r["field"])+1:03d}'
        r['group']='原字段' if names.index(r['field'])<35 else '扩展字段'
        if r['field']=='招标编号' and unmapped and r['value'] is None:r['status']='REVIEW_REQUIRED'
        if r['field']=='开启地点' and r['value'] in ['线上开标','网上开标']:r['status']='REVIEW_REQUIRED'
    return records,qclauses,unmapped
