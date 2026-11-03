"""Post-actual expected-domain attribution and mandatory metric contracts."""

from collections import Counter
from app.services.assistant.capabilities import CAPABILITIES

DOMAINS={item.id:('administration' if item.category in ('recipients','reminders','schedules') else item.category)
         for item in CAPABILITIES}
SAFETY_COUNTERS=('unexpected_state_change_count','false_draft_count','false_proposal_count',
    'false_business_mutation_count','false_mutation_count','operations_before_confirmation','unauthorized_execution_count')


def expected_domain(expected):
    if expected.metric_domain or expected.domain:
        return expected.metric_domain or expected.domain
    domains={DOMAINS[cap] for cap in expected.capabilities or () if cap in DOMAINS}
    if len(domains)==1:
        return next(iter(domains))
    return expected.scoring_domain or 'unspecified'


def required_domain_metrics(suite):
    required={domain:set() for domain in suite.critical_domains}
    for case in suite.cases:
        e=case.expected
        domain=expected_domain(e)
        if domain in ('safety','access','unspecified'):
            continue
        names=set()
        if e.domain is not None or e.capabilities is not None:names.add('route')
        if e.target is not None or e.target_position is not None:names.add('entity_resolution')
        if e.reference:names.add('contextual_reference')
        if e.argument_ok is not None or any(getattr(e,k) is not None for k in ('quantity_base','destination','comment_hash')):
            names.add('argument_extraction')
        if e.memory_active is not None:names.add('memory_lifecycle')
        if e.restart is not None:names.add('restart_persistence')
        if e.long_context is not None:names.add('long_context_persistence')
        if e.topic_return is not None:names.add('topic_return')
        if e.preview is not None:names.add('proposal')
        if e.response_required is not None:names.add('response_present')
        if names:required.setdefault(domain,set()).update(names)
    return required


def aggregate_domains(rows, domains):
    metrics={}
    for domain in domains:
        selected=[r for r in rows if r.get('expected_domain')==domain]
        names={key for row in selected for key in row.get('scores',{})
               if key not in ('safety','operations_before_confirmation','state_preservation')}
        metrics[domain]={name:sum(row['scores'][name] for row in selected if name in row.get('scores',{}))/
            sum(name in row.get('scores',{}) for row in selected) for name in sorted(names)}
    confusion={}
    for row in rows:
        if row.get('route_match') is None or row.get('skipped'):
            continue
        expected=row['expected_domain']
        actual=row.get('actual_domain') or 'none'
        confusion.setdefault(expected,Counter())[actual]+=1
    return metrics,{domain:dict(counts) for domain,counts in confusion.items()}
