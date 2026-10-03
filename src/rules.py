from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='职业辐射剂量与异常事件'; ENTITY='剂量事件'; ID_PREFIX='RD'
SEVERITIES=['low', 'elevated', 'high', 'critical']; STATES=['recorded', 'reviewing', 'investigation', 'follow_up', 'closed']; TRANSITIONS={'recorded': ['reviewing'], 'reviewing': ['investigation'], 'investigation': ['follow_up'], 'follow_up': ['closed'], 'closed': []}; TRANSITION_ROLES={'reviewing': ['radiation_officer'], 'investigation': ['radiation_officer'], 'follow_up': ['health_physicist'], 'closed': ['health_physicist']}
CREATE_ROLES=set(['dosimetrist']); RECORD_ROLES=set(['radiation_officer', 'health_physicist']); AUDIT_ROLES=set(['health_physicist', 'viewer']); VIEW_ROLES=set(['dosimetrist', 'radiation_officer', 'health_physicist', 'viewer'])
SEVERITY_WEIGHT={'low': 1.0, 'elevated': 3.0, 'high': 6.0, 'critical': 9.0}; DEADLINE_HOURS={'low': 72, 'elevated': 24, 'high': 8, 'critical': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))

# 批次重算链规则
BATCH_SOURCES=['dosimeter','officer','physicist']
SOURCE_ORDER={s:i for i,s in enumerate(BATCH_SOURCES)}
BATCH_STATUSES=['pending','confirmed','suspended','failed']
CONCLUSION_STATUSES=['valid','invalidated']
CONCLUSION_KINDS=['below_threshold','investigation_required']
DEFAULT_ANNUAL_THRESHOLD=20.0
DOSE_TOLERANCE=1e-9
BATCH_SUBMIT_ROLES=set(['dosimetrist','radiation_officer','health_physicist'])
BATCH_VIEW_ROLES=VIEW_ROLES
RECALCULATE_ROLES=set(['radiation_officer','health_physicist'])

def source_order(source):
    return SOURCE_ORDER.get(source,len(SOURCE_ORDER))

def backfill_annual_key(measured_at):
    """旧数据缺少年度键时按测量时刻回填年度键。"""
    text=str(measured_at).strip()
    if len(text)>=4 and text[:4].isdigit(): return text[:4]
    raise ValidationError("measured_at无法确定年度键")

def reading_key(reading):
    return (str(reading['person']),str(reading['period']),str(reading['measured_at']))

def readings_overlap(a,b):
    """同一人员、同一周期、同一测量时刻 → 重叠。"""
    return reading_key(a)==reading_key(b)

def doses_consistent(a,b):
    """重叠读数剂量一致（在容差内）。"""
    return abs(float(a['dose'])-float(b['dose']))<=DOSE_TOLERANCE

def merge_effective_doses(readings):
    """同一人员周期按来源顺序合并，返回 {period: effective_dose}。

    来源顺序最高的读数作为该周期的有效剂量，重放/重复提交不会重复计入。
    """
    effective={}
    for r in readings:
        period=str(r['period']); dose=float(r['dose']); order=source_order(r['source'])
        if period not in effective or order>effective[period][0]:
            effective[period]=(order,dose)
    return {p:v[1] for p,v in effective.items()}

def annual_totals(readings):
    """从读数重算年度累计：{annual_key: {total_dose, reading_count}}。

    年度累计由读数重算而非累加，因此重放不会重复计入。
    """
    by_year={}
    for r in readings:
        by_year.setdefault(str(r['annual_key']),[]).append(r)
    result={}
    for annual_key,year_readings in by_year.items():
        effective=merge_effective_doses(year_readings)
        result[annual_key]={'total_dose':sum(effective.values()),'reading_count':len(effective)}
    return result

def disposition_kind(total,threshold=DEFAULT_ANNUAL_THRESHOLD):
    if total>=threshold: return 'investigation_required'
    return 'below_threshold'

def conclusion_deadline_hours(total,threshold=DEFAULT_ANNUAL_THRESHOLD):
    return response_deadline_hours('high',total,threshold)
