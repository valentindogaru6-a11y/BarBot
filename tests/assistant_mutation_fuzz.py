"""Frozen synthetic grammar corpus; no provider, services or expected leakage."""
import hashlib
import itertools
import json
import random

SEED = 20261109
V11_SHA256 = '61625f501c4f6387bbf0c5ab5b1ec51597ffef53a6f3224526d24488a6572625'
ACTIONS = (('спиши','writeoff'),('списать','writeoff'),('списание:','writeoff'),
    ('списуй','writeoff'),('списання:','writeoff'),('не забудь списать','writeoff'),
    ('перемести','transfer'),('переместить','transfer'),('перемещение:','transfer'),
    ('перемісти','transfer'),('переміщення:','transfer'),('не забудь переместить','transfer'))
ENTITIES = (('апельсин','kg'),('лимон','kg'),('кокосовое молоко','l'),
    ('ягодная основа','l'),('отмена','kg'),('стоп','pcs'))
QUANTITIES = {'kg':(('200','г'),('0.2','кг'),('0,3','кг'),('1 200','г')),
    'l':(('200','мл'),('0.2','л'),('0,3','л'),('1 200','мл')),
    'pcs':(('1','шт'),('2','шт'),('3','шт'),('4','шт'))}


def corpus_v11():
    rng = random.Random(SEED)
    grid = list(itertools.product(ACTIONS,ENTITIES,range(4)))
    cases = []
    for index, ((verb,action),(entity,dimension),quantity_index) in enumerate(rng.sample(grid,64)):
        number,unit = QUANTITIES[dimension][quantity_index]
        dest = ' -> кухня' if action == 'transfer' else ''
        def command(n=number,u=unit):
            return f'{verb} {entity} {n} {u}'.strip()+dest
        base = command()
        new_number,new_unit = ('2','шт') if dimension == 'pcs' else ('300','г' if dimension == 'kg' else 'мл')
        variants = (
            ('base',base,'complete'),
            ('punct_case',base.upper()+'!','complete'),
            ('terminal_cancel',base+rng.choice((', ','; ',' — ','. ','\n'))+rng.choice(('отмена','отмени это','скасуй','стоп','хотя нет')),'blocked'),
            ('typo_one',base+', отмна','blocked'),
            ('typo_two',base+', атмна','blocked'),
            ('contradiction',base+', а потом отмени отмену','blocked'),
            ('zero_width_cancel',base+', отм'+rng.choice(('\u200b','\u200c','\u200d','\u2060'))+'ени','blocked'),
            ('confusable_cancel',base+', отмeни','blocked'),
            ('negative',command(rng.choice(('-','−','–','—'))+number),'blocked'),
            ('malformed',command(rng.choice(('1,2,3','1.2.3','1,,2'))),'blocked'),
            ('range',command('200–300'),'blocked'),
            ('correction',base+f', нет, {new_number} {new_unit}','corrected'),
            ('reason',base+', причина — отмена поставки','complete'),
            ('batch_invalid',base+'\n'+f'{entity} -200 {unit}'+dest,'blocked'),
            ('missing_quantity',command(''),'partial'),
            ('missing_unit',command(number,''),'partial'),
            ('final_new_action',base+', отмена, '+command(new_number,new_unit),'corrected'),
            ('oversize',command('999999999999999999999999999999'),'blocked'),
            ('precision',command('0.0000000001'),'blocked'),
            ('unicode_space',base.replace(' ',rng.choice(('\u00a0','\u2009'))),'complete'),
            ('reordered_cancel', 'отмена, '+base,'corrected'),
            ('newline_cancel',base+'\nотмена','blocked'),
        )
        for variant,text,expected in variants:
            cases.append(dict(case_id=f'G{index:03d}_{variant}',variant=variant,text=text,
                expected=expected,action=action,entity=entity))
    cases += [dict(case_id='BOUND_LENGTH',variant='bound',text='спиши апельсин 200 г'+('а'*4001),expected='blocked',action='writeoff',entity='апельсин'),
        dict(case_id='BOUND_CLAUSES',variant='bound',text='спиши апельсин 200 г'+(', он не свежий'*65),expected='blocked',action='writeoff',entity='апельсин')]
    assert hashlib.sha256(json.dumps(cases,ensure_ascii=False,sort_keys=True).encode()).hexdigest() == V11_SHA256
    return cases


CONTROLS = ('\u200b','\u200c','\u200d','\u2060','\ufeff','\u00ad','\u202a','\u202b',
    '\u202d','\u202e','\u202c','\u2066','\u2067','\u2068','\u2069','\u034f','\ufe0f',
    '\U000e0100','\x00','\x1b','\u0301')


def corpus_v12():
    """Separate extension: 210 control +100 entity +100 FSM role cases."""
    cases=[]
    for index in range(210):
        control=CONTROLS[index % len(CONTROLS)]
        amount=str(index+1)
        template,keyword=((f'спиши апельсин {amount} г, {{}}','отмени'),
            (f'{{}} апельсин {amount} г','спиши'),('спиши апельсин {} г',amount),
            (f'спиши апельсин {amount} {{}}','грамм'),(f'спиши {{}} {amount} г','апельсин'),
            (f'спиши апельсин {amount} г ({{}} )','причина'))[index % 6]
        pos=1+(index//len(CONTROLS)) % max(1,len(keyword)-1)
        text=template.format(keyword[:pos]+control+keyword[pos:])
        cases.append(dict(case_id=f'U{index:03d}',text=text,expected='blocked',family='unicode'))
    for index in range(100):
        name=('стоп','отмена','отмени','стоп-кран','напиток стоп')[index%5]
        action=('спиши','списать','списание:','не забудь списать')[index%4]
        unit='' if index%2 else ' г'
        cases.append(dict(case_id=f'E{index:03d}',text=f'{action} {name} {index+1}{unit}',
            expected='partial' if not unit else 'complete',family='entity'))
    for index in range(100):
        text=('спиши стоп 1','спиши стоп 1 г','стоп','спиши апельсин 1 г, стоп',
            'спиши стоп 1 г, нет, 2 г')[index%5]
        cases.append(dict(case_id=f'F{index:03d}',text=text,expected='fsm',family='fsm',
            restart=bool(index%2),actor=('owner','other','other_chat')[index%3],
            initial_quantity=index+1,
            initial_state=('comment_choice','comment_text','comment_confirm','resolving')[index%4]))
    return cases
