"""Reconstruct scoring rows from pdf-inspector coordinates without PyMuPDF tables."""
import re
from .document import View,clean


def page_headings(nodes):
    plain=[];paired=[]
    for node in nodes:
        text=clean(node['text'])
        combined=re.fullmatch(r'(?P<name>[\u4e00-\u9fff]{2,20})[（(]?(?P<score>\d+(?:\.\d+)?)分[）)]?',text)
        if combined and node['x']>=140:
            paired.append(dict(name=combined['name'],score=float(combined['score']),nodes=[node],center=node['y']))
        elif re.fullmatch(r'[\u4e00-\u9fff]{2,20}',text) and node['x']>=140:
            plain.append(node)
    for score_node in nodes:
        m=re.fullmatch(r'[（(](?P<score>\d+(?:\.\d+)?)分[）)]',clean(score_node['text']))
        if not m or score_node['x']<140:continue
        candidates=[n for n in plain if 0 < n['y']-score_node['y'] <= 35 and abs(n['x']-score_node['x'])<45]
        if not candidates:continue
        name_node=min(candidates,key=lambda n:n['y']-score_node['y'])
        paired.append(dict(name=clean(name_node['text']),score=float(m['score']),nodes=[name_node,score_node],
                           center=(name_node['y']+score_node['y'])/2))
    unique={}
    for item in paired:unique[(item['name'],item['score'])]=item
    return sorted(unique.values(),key=lambda x:-x['center'])


def segment(nodes,headings):
    """Assign contiguous right-column lines by matching each cell's vertical centre."""
    if not nodes or not headings:return [],nodes
    nodes=sorted(nodes,key=lambda n:-n['y']);n=len(nodes);k=len(headings);skip_penalty=10.0
    dp={(0,0):(0.0,[])}
    # Initial unassigned lines are allowed for a cross-page continuation.
    for start in range(1,n-k+1):dp[(start,0)]=(start*skip_penalty,[])
    for j,h in enumerate(headings):
        nxt={}
        for (start,count),(cost,ranges) in dp.items():
            if count!=j:continue
            remaining=k-j-1
            for end in range(start+1,n-remaining+1):
                centre=(nodes[start]['y']+nodes[end-1]['y'])/2
                value=(cost+(centre-h['center'])**2,ranges+[(start,end)])
                key=(end,j+1)
                if key not in nxt or value[0]<nxt[key][0]:nxt[key]=value
        dp.update(nxt)
    choices=[]
    for (end,count),(cost,ranges) in dp.items():
        if count==k:choices.append((cost+(n-end)*skip_penalty,ranges,end))
    _,ranges,end=min(choices,key=lambda x:x[0])
    assigned=[nodes[a:b] for a,b in ranges]
    used={id(x) for block in assigned for x in block}
    return assigned,[x for x in nodes if id(x) not in used]


def extract(doc,method):
    v=doc.evaluation;issues=[];items=[]
    categories=[]
    for m in re.finditer(r'(?P<name>商务部分|产品性能|技术部分|投标报价)[：:]?(?P<score>\d+(?:\.\d+)?)分',v.text):
        c=v.capture(m.start(),m.end(),m['name'],'评分分值构成')
        categories.append(dict(name=m['name'],max_score=float(m['score']),page=c['page'],evidence=c['evidence']))
    categories=list({(c['name'],c['max_score']):c for c in categories}.values())
    previous=None
    for page in doc.groups['evaluation']:
        page_nodes=[n for n in doc.nodes if n['page']==page and n['y']>=80]
        headings=page_headings(page_nodes)
        if not headings:continue
        threshold=max(n['x']+n['width'] for h in headings for n in h['nodes'])+5
        right=[n for n in page_nodes if n['x']>=threshold and all(n not in h['nodes'] for h in headings)]
        blocks,unassigned=segment(right,headings)
        # Text above the first local row is the continuation of the prior page's last row.
        prefix=[n for n in unassigned if n['y']>max(h['center'] for h in headings)]
        if prefix and previous:
            c=View(sorted(prefix,key=lambda n:-n['y'])).capture(0,len(View(sorted(prefix,key=lambda n:-n['y'])).text),'跨页续行','同一右侧评分标准列')
            previous['requirement']+=c['value'];previous['page']=sorted(set(previous['page']+c['page']));previous['evidence']+=c['evidence']
        for heading,block in zip(headings,blocks):
            rv=View(block);req=rv.capture(0,len(rv.text),heading['name'],'坐标表格右侧评分标准列')
            hv=View(heading['nodes']);head=hv.capture(0,len(hv.text),heading['name'],'坐标表格评审因素列')
            item=dict(item_id=f'score_{len(items)+1:03d}',name=heading['name'],max_score=heading['score'],category=None,
                      requirement=req['value'],page=sorted(set(head['page']+req['page'])),evidence=head['evidence']+req['evidence'],
                      sub_items=[],status='REVIEW_REQUIRED',note='评分项名称、分值和右侧连续标准按坐标列与单元格中心恢复。',
                      review={'status':'UNREVIEWED','value':None,'note':''})
            items.append(item);previous=item
    # The declared category totals partition the ordered item rows without relying on names.
    index=0
    for category in categories:
        total=0.0
        while index<len(items) and total<category['max_score']-0.001:
            items[index]['category']=category['name'];total+=items[index]['max_score'];index+=1
        if abs(total-category['max_score'])>0.001:issues.append(category['name']+'分值合计不一致')
    if index!=len(items):issues.append('存在未归入分值构成的评分项')
    checks=[]
    for cat in categories:
        total=sum(x['max_score'] for x in items if x['category']==cat['name'])
        checks.append(dict(category=cat['name'],declared=cat['max_score'],extracted=total,match=abs(total-cat['max_score'])<0.001))
    if not items:issues.append('未识别可用评分项')
    return dict(method=method,raw_text='\n'.join(n['text'] for n in v.nodes),raw_text_evidence=v.evidence(0,len(v.text)),
                score_categories=categories,score_items=items,extracted_total=sum(x['max_score'] for x in items),category_checks=checks,
                shared_rules_note='评分名称和分值来自评审因素列，requirement来自同行右侧评分标准列；跨页续行并入上一项。',issues=issues)
