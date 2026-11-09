"""Separate frozen service-word extension; v1.1/v1.2 are never modified."""
import hashlib
import json

V13_SHA256 = '99a82aeb28c58a0a23883c5546a9c9a2a5037f912ccbc5c7a6fd546497f84d5a'
ENTITIES = ('списание апельсин','перемещение лимон','стоп','отмена','списать',
    'переместить','коррекция','не списывать','списание списание','напиток списать апельсин',
    'апельсин списать','стоп отмена','перемести нектар','напиток перемещение',
    'отмена списание','продукт списать','списание апельсин списание','перемещение переместить')


def corpus_v13():
    cases=[]
    for index in range(360):
        entity=ENTITIES[(index//12+index)%len(ENTITIES)]
        action='writeoff' if index%12<6 else 'transfer'
        verb='спиши' if action=='writeoff' else 'перемести'
        header='списание:' if action=='writeoff' else 'перемещение:'
        unit='г' if action=='writeoff' else 'л'
        quantity=index+1
        destination=' -> кухня' if action=='transfer' else ''
        def command(amount=quantity, measure=unit, name=entity):
            return f'{verb} {name} {amount} {measure}'.rstrip()+destination
        variant=index%12
        expected='complete'
        text=command()
        if variant==1:
            text=text.replace(verb,header,1)
        elif variant==2:
            text=command(measure='');expected='partial'
        elif variant==3:
            text+=f', нет, {quantity+1} {unit}';expected='corrected'
        elif variant==4:
            text+=', стоп';expected='blocked'
        elif variant==5:
            text+=', затем перемести лимон 1 кг на кухню';expected='blocked'
        elif variant==6:
            text=f'{header}\n{entity} {quantity} {unit}'+destination
        elif variant==7:
            entity+=' списание';text=command(name=entity)
        elif variant==10:
            text=command(measure='');expected='partial'
        elif variant==11:
            text='не забудь '+command().replace(verb,'списать' if action=='writeoff' else 'переместить',1)
        cases.append(dict(case_id=f'R{index:03d}',variant=variant,text=text,entity=entity,
            action=action,quantity=quantity,unit=unit,expected=expected,
            mode=('local_first','ai_first')[(index//12)%2],
            restart=variant==10 or bool((index//24)%2),
            resolution='alias' if variant==8 else 'missing' if variant==9 else 'exact'))
    return cases


def checksum_v13():
    return hashlib.sha256(json.dumps(corpus_v13(),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
