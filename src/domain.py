from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message): super().__init__(message); self.message=message
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['low', 'elevated', 'high', 'critical']; STATES=['recorded', 'reviewing', 'investigation', 'follow_up', 'closed']; ROLES=['dosimetrist', 'radiation_officer', 'health_physicist', 'viewer']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
# 批次重算链实体
BATCH_STATUSES=['pending','confirmed','suspended','failed']; CONCLUSION_STATUSES=['valid','invalidated']
@dataclass(frozen=True)
class Batch:
    id:int; external_ref:str; source:str; status:str; content_hash:str; reading_count:int
    payload:Dict[str,Any]; submitted_by:str; submitted_at:str; confirmed_at:Optional[str]; detail:Dict[str,Any]
@dataclass(frozen=True)
class Reading:
    id:int; batch_id:int; external_ref:Optional[str]; person:str; period:str; measured_at:str
    dose:float; annual_key:str; source:str; created_at:str
@dataclass(frozen=True)
class AnnualTotal:
    person:str; annual_key:str; total_dose:float; reading_count:int; computed_at:str
@dataclass(frozen=True)
class Conclusion:
    id:int; person:str; annual_key:str; period:Optional[str]; kind:str; status:str
    result:Dict[str,Any]; computed_at:str; invalidated_at:Optional[str]
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
def require_person(value): return require_text(value,"person",100)
def require_period(value): return require_text(value,"period",50)
def require_measured_at(value):
    text=require_text(value,"measured_at",40)
    if len(text)<4 or not text[:4].isdigit(): raise ValidationError("measured_at必须以年份开头(ISO)")
    return text
def require_dose(value): return require_number(value,"dose",0.0)
def require_batch_source(value,sources):
    text=require_text(value,"source",50)
    if text not in sources: raise ValidationError(f"source必须是{sources}之一")
    return text
