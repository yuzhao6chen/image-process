"""Generic qualification extraction with semantic canonicalization."""
from hashlib import sha256
import re
from .document import View


def normalized(text):
    return re.sub(r'[\s，,。；;：:（）()\[\]【】]', '', text).replace('应当', '须').replace('应', '须')


def classify(text):
    if re.search(r'投融资能力|资金筹措|净资产不低于|融资能力', text): return 'FINANCIAL', '投融资能力要求', 'financing_capacity'
    if re.search(r'财务会计制度|财务|审计报告|资产负债|流动比率|破产|亏损', text): return 'FINANCIAL', '财务要求', 'financial'
    if re.search(r'依法设立|依法注册|独立承担民事责任', text): return 'BASIC', '基本资格要求', 'legal_person'
    if re.search(r'联合体', text): return 'CONSORTIUM', '联合体要求', 'consortium'
    if re.search(r'省外|外省|入晋|入省|跨省|外地企业', text):
        if re.search(r'截图|登记|备案', text): return 'REGIONAL_ACCESS', '省外登记备案', 'regional_registration'
        return 'REGIONAL_ACCESS', '省外企业平台准入', 'regional_platform'
    if re.search(r'授权委托|法定代表人身份证|被授权人身份证', text): return 'AUTHORIZATION', '授权委托及身份证明', 'authorization'
    if re.search(r'基本账户|基本存款账户|开户许可证', text): return 'BANK_ACCOUNT', '基本账户证明', 'bank_account'
    licence = re.search(r'([\u4e00-\u9fff]{0,12}许可证)', text)
    if licence: return 'LICENCE', licence[1], 'licence:' + licence[1]
    if re.search(r'信用中国|失信被执行人|严重违法失信|信用信息公示|行贿犯罪|黑名单|信用评价|不良行为记录|商业信誉|被责令停业|吊销执照|不良信用记录|不良状况|重大违法记录|取消.+投标资格', text):
        return 'CREDIT', '信用及失信限制', 'credit_bribery' if '行贿' in text else 'credit_platform'
    if re.search(r'业绩|类似项目|合同额|累计完成', text):
        role = re.search(r'(企业|投标人|项目经理|项目负责人|设计负责人|总监)', text)
        return 'PERFORMANCE', '业绩要求', 'performance:' + (role[1] if role else 'bidder')
    if re.search(r'单位负责人|控股|管理关系|利害关系|关联关系|投资参股|运营机构.+?投标', text):
        return 'RELATIONSHIP', '关联关系限制', 'platform_relationship' if re.search(r'平台|运营机构', text) else 'ownership_relationship'
    if re.search(r'项目经理|项目负责人|技术负责人|设计负责人|施工负责人|总监理工程师|建造师|拟派人员|社保', text):
        role = re.search(r'(项目经理|项目负责人|技术负责人|设计负责人|施工负责人|总监理工程师|建造师)', text)
        identity = 'personnel:' + (role[1] if role else sha256(normalized(text).encode()).hexdigest()[:10])
        return 'PERSONNEL', role[1] if role else '人员要求', identity
    qualification = re.search(r'([\u4e00-\u9fff]{2,30}(?:施工总承包|专业承包|设计资质|勘察资质|监理资质|工程咨询|工程设计|工程勘察)[\u4e00-\u9fff甲乙丙壹贰叁一二三级综合行业专项]*资质?)', text)
    if qualification:
        return 'PROFESSIONAL_QUALIFICATION', '企业专业资质', 'qualification:' + normalized(qualification[1])
    if re.search(r'资质证书|资质等级|相应资质', text):
        return 'PROFESSIONAL_QUALIFICATION', '企业专业资质', 'qualification:' + sha256(normalized(text).encode()).hexdigest()[:10]
    if re.search(r'独立法人|法人资格|营业执照|依法设立|依法注册|实施能力|施工能力|承担能力|独立承担民事责任|履行合同所必需|依法缴纳税收|社会保障资金|法律、行政法规规定', text):
        identity = ('business_license' if '营业执照' in text else
                    'civil_liability' if '独立承担民事责任' in text else
                    'tax_social_security' if re.search(r'缴纳税收|社会保障资金',text) else
                    'contract_capacity' if '履行合同所必需' in text else
                    'implementation_capacity' if '能力' in text else 'legal_person')
        return 'BASIC', '基本资格要求', identity
    return 'OTHER', '其他资格要求', 'other:' + sha256(normalized(text).encode()).hexdigest()[:12]


def split_ranges(text, start, end):
    block = text[start:end]
    cuts = [0]
    for match in re.finditer(
            r'(?:[；;。](?=\d+(?:\.\d+)+|\d+[、.]|[（(]\d+[）)])|(?=\d+(?:\.\d+)+)|(?=\d+[、.])|(?=[（(]\d+[）)])|'
            r'(?=其中[，,]?(?:投标人)?拟派项目经理)|(?=有效的营业执照)|(?=本次招标项目不接受联合体)|'
            r'(?=法定代表人或单位负责人)|(?=(?<!或)单位负责人为同一人)|(?=未列入全国建筑市场)|'
            r'(?=[£□☑√]*不接受[£□☑√]*接受))', block):
        cuts.append(match.start())
    cuts.append(len(block))
    ranges = []
    for a, b in zip(cuts, cuts[1:]):
        while a < b and block[a] in '；;。:：、0123456789.（）()': a += 1
        while b > a and block[b - 1] in '；;。': b -= 1
        if b - a >= 4: ranges.append((start + a, start + b))
    return ranges or [(start, end)]


def extract(doc, clauses):
    source_items, raw_blocks, seen_ranges = [], [], set()

    def add_range(view, start, end, role):
        signature = (id(view), start, end)
        if end <= start or signature in seen_ranges: return
        seen_ranges.add(signature); raw_blocks.append((view, start, end))
        for a, b in split_ranges(view.text, start, end):
            capture = view.capture(a, b, '资格要求', '资格段落及编号边界')
            requirement = re.sub(r'^(?:资质条件|资格要求|投标人资格要求)[：:]?', '', capture['value'])
            if len(requirement) < 4 or re.fullmatch(r'(?:无|不要求|详见.+)', requirement): continue
            if re.fullmatch(r'(?:\d{3})?(?:第一标段|不分标段|本项目的特定资格要求|本次招标要求申请人应具备以下资格条件|法人资格|业绩资格要求|商业信誉的要求|投融资能力)[：:]?', requirement): continue
            if re.fullmatch(r'业绩要求[：:]?[\\/]*',requirement):continue
            typ, name, identity = classify(requirement)
            level = re.search(r'([特壹贰叁一二三甲乙丙]级(?:及以上)?)', requirement)
            qname = re.search(r'([\u4e00-\u9fff]{2,25}(?:施工总承包|专业承包|设计资质|勘察资质|监理资质)[\u4e00-\u9fff甲乙丙壹贰叁一二三级]*)', requirement)
            source_items.append({
                'type': typ, 'name': name, 'identity': identity, 'requirement': requirement,
                'page': capture['page'], 'evidence': capture['evidence'], 'source_role': role,
                'qualification_name': qname[1] if qname and typ == 'PROFESSIONAL_QUALIFICATION' else None,
                'level': level[1] if level and typ in {'PROFESSIONAL_QUALIFICATION', 'PERSONNEL'} else None,
            })

    if clauses:
        for clause in clauses: add_range(doc.announcement, clause['start'], clause['end'], 'ANNOUNCEMENT')
    else:
        text = doc.announcement.text
        for match in list(re.finditer(r'本次招标要求投标人|投标人资格要求|投标人应具备', text))[:2]:
            stop = re.search(r'招标文件(?:的)?获取|投标文件(?:的)?递交|发布公告', text[match.start() + 5:])
            end = match.start() + 5 + stop.start() if stop else min(len(text), match.start() + 2500)
            add_range(doc.announcement, match.start(), end, 'ANNOUNCEMENT')
    front = doc.front_content
    for marker in ['资质条件：', '资质条件:', '投标人资格条件：', '投标人资格要求：',
                   '投标人资质、能力和信誉']:
        start = front.text.find(marker)
        if start >= 0:
            tail = front.text[start + len(marker):]
            stop = re.search(r'投标截止时间|是否接受联合体|踏勘现场|投标预备会', tail)
            end = start + len(marker) + stop.start() if stop else min(len(front.text), start + 2200)
            add_range(front, start + len(marker), end, 'FRONT_TABLE'); break
    if not source_items:
        full_front=re.search(r'(?P<v>[（(]1[）)]资质要求[：:].+?)(?=☑不组织|□不组织|踏勘现场)',front.text)
        if full_front:add_range(front,full_front.start('v'),full_front.end('v'),'FRONT_TABLE')
    if not source_items:
        by_id={node['item_id']:node for node in doc.nodes}
        for label in ['投标人资质、能力和信誉','投标人资格要求','申请人资格要求']:
            candidates=doc.row_band_values(label)
            if not candidates:continue
            ordered=[];seen=set()
            for evidence in candidates[0]['evidence']:
                item_id=evidence['item_id']
                if item_id in by_id and item_id not in seen:
                    ordered.append(by_id[item_id]);seen.add(item_id)
            if ordered:
                value_view=View(ordered)
                add_range(value_view,0,len(value_view.text),'FRONT_TABLE')
            break

    for index, item in enumerate(source_items, 1): item['source_item_id'] = f'qualification_source_{index:03d}'
    groups = {}
    for item in source_items: groups.setdefault((item['type'], item['identity']), []).append(item)
    items = []
    for (_, identity), sources in groups.items():
        texts = [normalized(x['requirement']) for x in sources]
        selected = max(sources, key=lambda x: len(normalized(x['requirement'])))
        if len(sources) == 1: merge = 'NONE'
        elif len(set(texts)) == 1: merge = 'EXACT_DUPLICATE'
        elif any(a in b for a in texts for b in texts if a != b): merge = 'SUPPLEMENT'
        else: merge = 'SEMANTIC_DUPLICATE'
        levels = {x['level'] for x in sources if x['level']}
        items.append({
            'canonical_item_id': 'qual_' + sha256(identity.encode()).hexdigest()[:16],
            'type': selected['type'], 'name': selected['name'], 'requirement': selected['requirement'],
            'qualification_name': selected['qualification_name'],
            'level': next(iter(levels)) if len(levels) == 1 else None,
            'page': sorted({p for x in sources for p in x['page']}), 'merge_type': merge,
            'evidences': [{**e, 'source_item_id': x['source_item_id'], 'source_role': x['source_role']}
                          for x in sources for e in x['evidence']],
        })
    all_evidence = [e for view, start, end in raw_blocks for e in view.evidence(start, end)]
    return {'raw_text': '\n'.join(view.text[start:end] for view, start, end in raw_blocks),
            'raw_text_evidence': all_evidence, 'items': items, 'source_items': source_items,
            'issues': [] if items else ['未定位明确资格结构化项']}
