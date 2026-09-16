"""Admission requirements only; typed identities rather than type-only deduplication."""
from hashlib import sha256
import re
from .document import clean


def extract(doc,clauses):
    raw=[]
    def add(view,pattern,typ,name,identity):
        for m in re.finditer(pattern,view.text):
            c=view.capture(m.start('v'),m.end('v'),name,'资格语义片段')
            item=dict(type=typ,name=name,requirement=c['value'].rstrip('；;。'),page=c['page'],evidence=c['evidence'],identity=identity)
            level=re.search(r'([壹贰叁一二三甲乙丙特]级(?:及以上)?)',item['requirement'])
            item['level']=level[1] if level and typ in ['PROFESSIONAL_QUALIFICATION','PERSONNEL'] else None
            qname=re.search(r'([\u4e00-\u9fff]{2,12}(?:施工总承包|专业承包))',item['requirement'])
            item['qualification_name']=re.sub(r'^.*(?:颁发的|具备|具有)','',qname[1]) if qname else '注册建造师' if typ=='PERSONNEL' and '注册建造师' in item['requirement'] else None
            raw.append(item)
    from .document import View
    if not clauses:return dict(raw_text='',items=[],source_items=[],issues=['未定位公告资格条款'])
    parent=doc.announcement.capture(clauses[0]['start'],clauses[-1]['end'])
    # All grammar matching runs only within admission paragraphs and the front table's qualification cell.
    ann_start=clauses[0]['start'];ann_end=clauses[-1]['end']
    class Sliced:
        text=doc.announcement.text[ann_start:ann_end]
        def capture(self,a,b,*args):return doc.announcement.capture(ann_start+a,ann_start+b,*args)
    a=Sliced();f=doc.front_content
    start=f.text.find('资质条件:')
    if start<0:start=f.text.find('资质条件：')
    end=f.text.find('☑不接受',start)
    if end<0:end=f.text.find('投标截止时间十五日前',start)
    class FrontSlice:
        text=f.text[start:end] if start>=0 and end>start else ''
        def capture(self,a,b,*args):return f.capture(start+a,start+b,*args)
    front=FrontSlice()
    for view in [a,front]:
        add(view,r'(?P<v>(?:本次招标要求)?投标人应在.+?具有独立法人资格)','BASIC','独立法人资格','legal_person')
        add(view,r'(?P<v>具备建设行政主管部门颁发的.+?(?:施工总承包|专业承包)[壹贰叁一二三甲乙丙特]级及以上资质)','PROFESSIONAL_QUALIFICATION','企业专业资质','enterprise_qualification')
        add(view,r'(?P<v>有效的营业执照)','BASIC','营业执照','business_license')
        add(view,r'(?P<v>安全生产许可证)(?=[，,；;])','LICENCE','安全生产许可证','safety_license')
        add(view,r'(?P<v>并在人员、设备、资金等方面具有相应的施工能力)','BASIC','施工实施能力','implementation_capacity')
        add(view,r'(?P<v>投标人拟派项目经理须具备.+?且未担任其它在建工程项目的项目经理)','PERSONNEL','项目经理资格及在建限制','project_manager')
        add(view,r'(?P<v>投标人在“信用中国”.+?名单（黑名单）”)(?=[，,、；;])','CREDIT','信用查询与失信限制','credit_systems')
        add(view,r'(?P<v>外省企业需提供[^；;，,。]{2,60}省外入[\u4e00-\u9fff]登记截图)','REGIONAL_ACCESS','省外登记截图','outside_registration')
        add(view,r'(?P<v>投标人如为外省企业需登陆“[^”]+”，按照要求完成录入企业基本信息等工作)','REGIONAL_ACCESS','省外企业平台信息录入','outside_platform_entry')
    add(a,r'(?P<v>单位负责人为同一人.+?不得同时参加本项目投标)','RELATIONSHIP','负责人及控股管理关系限制','ownership_relationship')
    add(a,r'(?P<v>本项目不接受联合体投标)','CONSORTIUM','联合体要求','consortium')
    add(a,r'(?P<v>电子招标投标交易平台的运营机构.+?不得在该交易平台进行的招标项目中投标或代理投标)','RELATIONSHIP','交易平台关联方限制','platform_relationship')
    add(front,r'(?P<v>业绩要求[：:]无)','PERFORMANCE','准入业绩要求','admission_performance')
    proof_ids={'营业执照':'business_license','资质证书':'enterprise_qualification','安全生产许可证':'safety_license',
               '基本账户':'bank_account','财务审计报告':'financial_audit','项目经理':'project_manager','信用中国':'credit_systems','法人授权委托书':'authorization'}
    for m in re.finditer(r'（\d+）(?P<v>.+?)(?=（\d+）|$)',front.text):
        value=m['v'].rstrip('；;。')
        identity=next((identity for word,identity in proof_ids.items() if word in value),'proof_'+sha256(value.encode()).hexdigest()[:8])
        existing=next((x for x in raw if x['identity']==identity),None)
        default_types={'financial_audit':'FINANCIAL','bank_account':'BANK_ACCOUNT','authorization':'AUTHORIZATION','safety_license':'LICENCE'}
        typ=existing['type'] if existing else default_types.get(identity,'OTHER')
        name=existing['name'] if existing else {'financial_audit':'财务审计报告','bank_account':'基本账户证明','authorization':'授权委托及身份证明'}.get(identity,'其他资格证明')
        c=front.capture(m.start('v'),m.start('v')+len(value),name,'资格证明材料清单')
        raw.append(dict(type=typ,name=name,requirement=value,page=c['page'],evidence=c['evidence'],identity=identity,level=None,qualification_name=None))
    for i,item in enumerate(raw,1):item['source_item_id']=f'qualification_source_{i:03d}'
    groups={}
    for item in raw:
        # Known identity is refined by actual enterprise qualification name where present.
        identity=item['identity']
        if identity=='enterprise_qualification' and item['qualification_name']:identity+=':'+item['qualification_name']
        # Generic proof can merge only when exactly one specific qualification is present.
        if identity=='enterprise_qualification':
            specific={x['qualification_name'] for x in raw if x['identity']=='enterprise_qualification' and x['qualification_name']}
            if len(specific)==1:identity+=':'+next(iter(specific))
        groups.setdefault((item['type'],identity),[]).append(item)
    items=[]
    for (_,identity),sources in groups.items():
        parts=[];merge='NONE';levels={s['level'] for s in sources if s['level']}
        for s in sources:
            value=s['requirement']
            if value in parts:merge='EXACT_DUPLICATE' if merge=='NONE' else merge
            elif any(value in p for p in parts):merge='SUPPLEMENT'
            else:
                if any(p in value and p!=value for p in parts):merge='SUPPLEMENT'
                parts=[p for p in parts if p not in value];parts.append(value)
                if len(sources)>1 and len(parts)>1:merge='SUPPLEMENT'
        cid='qual_'+sha256(identity.encode()).hexdigest()[:16]
        ev=[{**e,'source_item_id':s['source_item_id']} for s in sources for e in s['evidence']]
        items.append(dict(canonical_item_id=cid,type=sources[0]['type'],name=sources[0]['name'],
            requirement='；'.join(parts),requirement_parts=parts,raw_text='\n'.join(e['text'] for e in ev),
            evidences=ev,page=sorted({e['page'] for e in ev}),merge_type=merge,
            qualification_name=next((s['qualification_name'] for s in sources if s['qualification_name']),None),
            level=next(iter(levels)) if len(levels)==1 else None,
            status='CONFLICT' if len(levels)>1 else 'REVIEW_REQUIRED',
            note='逐来源原文片段合并，未总结改写；需确认资质类型、补充关系及完整性。'+('存在不同等级候选，不自动选择。' if len(levels)>1 else ''),
            review={'status':'UNREVIEWED','value':None,'note':''}))
    return dict(raw_text='\n'.join(e['text'] for e in parent['evidence']),raw_text_evidence=parent['evidence'],
                items=items,source_items=raw,issues=['资格项分类、拆分、证明材料合并均未经人工审核；评分业绩不作为准入要求。'])
