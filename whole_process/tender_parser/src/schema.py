from .qualification_types import QUALIFICATION_TYPES

TYPES=QUALIFICATION_TYPES
STATUS=['EXTRACTED','MISSING','CONFLICT','REVIEW_REQUIRED']


def schema(names):
    evidence={'type':'object','required':['item_id','page','start','end','text','section','bbox'],
              'properties':{'page':{'type':'integer','minimum':1},'start':{'type':'integer','minimum':0},
                            'end':{'type':'integer','minimum':1},'text':{'type':'string','minLength':1}}}
    proof={'type':'array','items':evidence}
    review={'type':'object','required':['status','value','note'],'properties':{'status':{'const':'UNREVIEWED'},'value':{'type':'null'}}}
    qual={'type':'object','required':['raw_text','items','source_items'],'properties':{'raw_text':{'type':'string'},
        'items':{'type':'array','items':{'type':'object','required':['canonical_item_id','type','name','requirement','evidences','merge_type','review'],
        'properties':{'canonical_item_id':{'type':'string'},'type':{'enum':TYPES},'name':{'type':'string'},'requirement':{'type':'string','minLength':1},
                      'evidences':{**proof,'minItems':1},'merge_type':{'enum':['EXACT_DUPLICATE','SEMANTIC_DUPLICATE','SUPPLEMENT','NONE']},'review':review}}}}}
    return {'$schema':'https://json-schema.org/draft/2020-12/schema','type':'object','required':['schema_version','source_pdf_sha256','training_ready','fields'],
       'properties':{'schema_version':{'const':'0.3.4'},'training_ready':{'const':False},'ocr_performed':{'const':False},
       'fields':{'type':'array','minItems':len(names),'maxItems':len(names),'items':{'type':'object',
          'required':['field','field_id','value','status','page','evidence','candidates','review'],
          'properties':{'field':{'enum':names},'status':{'enum':STATUS},'evidence':proof,'review':review,
                        'value':{'oneOf':[{'type':['string','null']},qual]}}}}}}
