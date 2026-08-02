"""Fail-closed validation for the six MVP manifest types."""
from __future__ import annotations
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable
import json, re
from .models import Diagnostic, Manifest, load_manifest
from .naming import SHA256_RE, TOKEN_RE, VERSION_RE, export_paths, safe_relative_path

KINDS={"character-spec","style-profile","generation-request","candidate-asset","review-decision","export-manifest"}
LICENSE_STATES={"unreviewed","reviewing","approved","rejected"}
REVIEW_STATES={"shortlist","accept","reject","needs_revision"}
ID_RE=re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
REF_RE=re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*@v[0-9]{3}$")
UTC_RE=re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REQUIRED={
"character-spec":("kind","schema_version","id","version","role","review_status","identity_anchors","license_status"),
"style-profile":("kind","schema_version","id","version","line","palette","anti_ai_checks","license_status"),
"generation-request":("kind","schema_version","id","character_ref","style_ref","pose","expression","crop","facing","tool_id","model_id","license_status","config","output_intent","provenance"),
"candidate-asset":("kind","schema_version","id","request_ref","path","sha256","width","height","color_space","has_alpha","media_type","status","provenance"),
"review-decision":("kind","schema_version","id","candidate_ref","candidate_request_ref","candidate_sha256","decision","reviewer","timestamp","categories"),
"export-manifest":("kind","schema_version","id","character_ref","candidate_ref","review_ref","path","sidecar_path","sha256","width","height","color_space","has_alpha","format","license_status","status","crop","facing","pose","expression","version"),
}
OPTIONAL={"seed","notes","supersedes","observed_sha256","tool_version"}

def _d(m:Manifest,code:str,msg:str,field:str="")->Diagnostic:return Diagnostic(code,msg,str(m.source),field)
def _ref(value:str)->tuple[str,str]:return tuple(value.rsplit("@",1)) # type: ignore[return-value]
def _text(value:Any)->bool:return isinstance(value,str) and bool(value.strip())

def validate_document(m:Manifest)->list[Diagnostic]:
 d=m.data; out:list[Diagnostic]=[]; kind=m.kind
 if kind not in KINDS:return [_d(m,"UNKNOWN_KIND",f"unsupported kind: {kind!r}","kind")]
 unknown=sorted(set(d)-set(REQUIRED[kind])-OPTIONAL)
 if unknown:out.append(_d(m,"UNKNOWN_FIELD",f"unknown fields are not accepted: {', '.join(unknown)}"))
 for f in REQUIRED[kind]:
  if f not in d:out.append(_d(m,"MISSING_FIELD","required field is missing",f))
 if d.get("schema_version")!="1.0":out.append(_d(m,"SCHEMA_VERSION","schema_version must be '1.0'","schema_version"))
 if "id" in d and (not isinstance(d["id"],str) or not ID_RE.fullmatch(d["id"])):out.append(_d(m,"INVALID_ID","id must be lowercase ASCII with hyphens","id"))
 for f in ("character_ref","style_ref"):
  if f in d and (not isinstance(d[f],str) or not REF_RE.fullmatch(d[f])):out.append(_d(m,"INVALID_REFERENCE","reference must use id@vNNN",f))
 for f in ("request_ref","candidate_ref","candidate_request_ref","review_ref"):
  if f in d and (not isinstance(d[f],str) or not ID_RE.fullmatch(d[f])):out.append(_d(m,"INVALID_REFERENCE","reference must be a manifest id",f))
 if "version" in d and (not isinstance(d["version"],str) or not VERSION_RE.fullmatch(d["version"])):out.append(_d(m,"INVALID_VERSION","version must use vNNN","version"))
 if "license_status" in d and d["license_status"] not in LICENSE_STATES:out.append(_d(m,"LICENSE_STATUS","invalid license_status","license_status"))
 if kind=="character-spec":
  if d.get("role") not in {"boke","tsukkomi"}:out.append(_d(m,"ROLE","role must be boke or tsukkomi","role"))
  if d.get("review_status") not in {"draft","approved","rejected"}:out.append(_d(m,"REVIEW_STATUS","invalid review_status","review_status"))
  if not isinstance(d.get("identity_anchors"),list) or not d.get("identity_anchors"):out.append(_d(m,"IDENTITY_ANCHORS","at least one identity anchor is required","identity_anchors"))
 if kind=="style-profile" and (not isinstance(d.get("anti_ai_checks"),list) or not d.get("anti_ai_checks")):out.append(_d(m,"ANTI_AI_CHECKS","anti_ai_checks must be a non-empty list","anti_ai_checks"))
 if kind=="generation-request":
  for f in ("pose","expression","crop","facing","tool_id","model_id"):
   if not isinstance(d.get(f),str) or not TOKEN_RE.fullmatch(d.get(f,"")):out.append(_d(m,"INVALID_TOKEN","must be a lowercase ASCII token",f))
  if not isinstance(d.get("config"),dict):out.append(_d(m,"CONFIG","config must be an object","config"))
  if not isinstance(d.get("provenance"),dict) or not _text(d.get("provenance",{}).get("source")):out.append(_d(m,"UNKNOWN_PROVENANCE","provenance.source is required","provenance"))
  seed=d.get("seed")
  if seed is not None and (not isinstance(seed,int) or isinstance(seed,bool) or seed<0):out.append(_d(m,"SEED","seed must be a non-negative integer","seed"))
 if kind in {"candidate-asset","export-manifest"}:
  for f in ("path","sidecar_path"):
   if f in d:
    try:safe_relative_path(d[f])
    except (TypeError,ValueError) as e:out.append(_d(m,"UNSAFE_PATH",str(e),f))
  if not isinstance(d.get("sha256"),str) or not SHA256_RE.fullmatch(d.get("sha256","")):out.append(_d(m,"CHECKSUM","sha256 must be 64 lowercase hexadecimal characters","sha256"))
  for f in ("width","height"):
   v=d.get(f)
   if not isinstance(v,int) or isinstance(v,bool) or v<=0:out.append(_d(m,"DIMENSION","dimension must be a positive integer",f))
  if d.get("color_space")!="sRGB":out.append(_d(m,"COLOR_SPACE","color_space must be sRGB","color_space"))
  if d.get("has_alpha") is not True:out.append(_d(m,"ALPHA_REQUIRED","transparent alpha is required","has_alpha"))
 if kind=="candidate-asset":
  if d.get("media_type")!="image/png":out.append(_d(m,"MEDIA_TYPE","media_type must be image/png","media_type"))
  if d.get("status") not in {"received","technically_valid","invalid"}:out.append(_d(m,"CANDIDATE_STATUS","invalid candidate status","status"))
  if not isinstance(d.get("provenance"),dict) or not _text(d.get("provenance",{}).get("source")):out.append(_d(m,"UNKNOWN_PROVENANCE","provenance.source is required","provenance"))
 if kind=="review-decision":
  if d.get("decision") not in REVIEW_STATES:out.append(_d(m,"DECISION","invalid review decision","decision"))
  if not _text(d.get("reviewer")):out.append(_d(m,"REVIEWER","reviewer is required","reviewer"))
  if not isinstance(d.get("timestamp"),str) or not UTC_RE.fullmatch(d.get("timestamp","")):out.append(_d(m,"TIMESTAMP","timestamp must be UTC YYYY-MM-DDTHH:MM:SSZ","timestamp"))
  if not isinstance(d.get("categories"),list):out.append(_d(m,"CATEGORIES","categories must be a list","categories"))
  if not isinstance(d.get("candidate_sha256"),str) or not SHA256_RE.fullmatch(d.get("candidate_sha256","")):out.append(_d(m,"CHECKSUM","candidate_sha256 must be 64 lowercase hexadecimal characters","candidate_sha256"))
 if kind=="export-manifest":
  if d.get("format")!="png":out.append(_d(m,"FORMAT","format must be png","format"))
  if d.get("status") not in {"planned","validated","packaged","verified"}:out.append(_d(m,"EXPORT_STATUS","invalid export status","status"))
  if d.get("license_status")!="approved":out.append(_d(m,"LICENSE_NOT_APPROVED","exports require approved licensing","license_status"))
  for f in ("crop","facing","pose","expression"):
   if not isinstance(d.get(f),str) or not TOKEN_RE.fullmatch(d.get(f,"")):out.append(_d(m,"INVALID_TOKEN","must be a lowercase ASCII token",f))
  needed=("character_ref","crop","facing","pose","expression","version","sha256","path","sidecar_path")
  if all(f in d for f in needed):
   try:
    cid,_=_ref(d["character_ref"]); path,sidecar=export_paths(character_id=cid,crop=d["crop"],facing=d["facing"],pose=d["pose"],expression=d["expression"],version=d["version"],sha256=d["sha256"])
    if d["path"]!=path:out.append(_d(m,"NONDETERMINISTIC_PATH",f"expected {path}","path"))
    if d["sidecar_path"]!=sidecar:out.append(_d(m,"NONDETERMINISTIC_PATH",f"expected {sidecar}","sidecar_path"))
   except (TypeError,ValueError):pass
 return out

def load_path(path:Path)->tuple[list[Manifest],list[Diagnostic]]:
 docs:list[Manifest]=[]; errors:list[Diagnostic]=[]; paths=[path] if path.is_file() else sorted(path.rglob("*.json"))
 if not paths:return docs,[Diagnostic("NO_DOCUMENTS","no JSON documents found",str(path))]
 for item in paths:
  try:docs.append(load_manifest(item))
  except (OSError,UnicodeError,json.JSONDecodeError,ValueError) as e:errors.append(Diagnostic("LOAD_ERROR",str(e),str(item)))
 return docs,errors

def validate_set(manifests:Iterable[Manifest])->list[Diagnostic]:
 docs=list(manifests); out:list[Diagnostic]=[]; index:dict[tuple[str,str],Manifest]={}; duplicate:dict[str,list[Manifest]]=defaultdict(list)
 for m in docs:
  out.extend(validate_document(m)); duplicate[m.manifest_id].append(m)
  if m.kind and m.manifest_id:index[(m.kind,m.manifest_id)]=m
 for ident,matches in duplicate.items():
  if ident and len(matches)>1:
   for m in matches:out.append(_d(m,"DUPLICATE_ID",f"duplicate manifest id: {ident}","id"))
 def need(kind:str,ident:str,source:Manifest,field:str)->Manifest|None:
  target=index.get((kind,ident))
  if target is None:out.append(_d(source,"UNRESOLVED_REFERENCE",f"{kind} {ident!r} was not found",field))
  return target
 for m in docs:
  d=m.data
  if m.kind=="generation-request":
   for f,k in (("character_ref","character-spec"),("style_ref","style-profile")):
    v=d.get(f)
    if isinstance(v,str) and REF_RE.fullmatch(v):
     ident,version=_ref(v); target=need(k,ident,m,f)
     if target and target.data.get("version")!=version:out.append(_d(m,"VERSION_MISMATCH",f"{f} version does not match target",f))
  elif m.kind=="candidate-asset" and isinstance(d.get("request_ref"),str):need("generation-request",d["request_ref"],m,"request_ref")
  elif m.kind=="review-decision":
   candidate=need("candidate-asset",d.get("candidate_ref",""),m,"candidate_ref")
   if candidate:
    if d.get("decision") in {"accept","shortlist"} and candidate.data.get("status")!="technically_valid":out.append(_d(m,"NOT_REVIEW_READY","candidate must be technically_valid","candidate_ref"))
    if d.get("candidate_request_ref")!=candidate.data.get("request_ref"):out.append(_d(m,"REVIEW_SOURCE_MISMATCH","review request must match candidate source request","candidate_request_ref"))
    if d.get("candidate_sha256")!=candidate.data.get("sha256"):out.append(_d(m,"REVIEW_CHECKSUM_MISMATCH","review checksum must match candidate","candidate_sha256"))
  elif m.kind=="export-manifest":
   candidate=need("candidate-asset",d.get("candidate_ref",""),m,"candidate_ref"); review=need("review-decision",d.get("review_ref",""),m,"review_ref"); request=None
   if candidate and isinstance(candidate.data.get("request_ref"),str):request=need("generation-request",candidate.data["request_ref"],m,"candidate_ref")
   if review and review.data.get("decision")!="accept":out.append(_d(m,"NOT_APPROVED","export review must be accept","review_ref"))
   if review and review.data.get("candidate_ref")!=d.get("candidate_ref"):out.append(_d(m,"REFERENCE_MISMATCH","review does not approve this candidate","review_ref"))
   if candidate:
    for f in ("sha256","width","height","color_space","has_alpha"):
     if candidate.data.get(f)!=d.get(f):out.append(_d(m,"EXPORT_MISMATCH",f"{f} differs from candidate",f))
    if candidate.data.get("status")!="technically_valid":out.append(_d(m,"NOT_REVIEW_READY","candidate is not technically_valid","candidate_ref"))
   if review and candidate:
    if review.data.get("candidate_request_ref")!=candidate.data.get("request_ref"):out.append(_d(m,"REVIEW_SOURCE_MISMATCH","review request must match candidate source request","review_ref"))
    sha=review.data.get("candidate_sha256")
    if sha!=candidate.data.get("sha256") or sha!=d.get("sha256"):out.append(_d(m,"REVIEW_CHECKSUM_MISMATCH","review checksum must match candidate and export","review_ref"))
   if request:
    for f in ("character_ref","pose","expression","crop","facing"):
     if request.data.get(f)!=d.get(f):out.append(_d(m,"SOURCE_METADATA_MISMATCH",f"{f} differs from source request",f))
    if request.data.get("license_status")!="approved":out.append(_d(m,"SOURCE_NOT_APPROVED","source request licensing is not approved","candidate_ref"))
    for f,k in (("character_ref","character-spec"),("style_ref","style-profile")):
     value=request.data.get(f)
     if isinstance(value,str) and REF_RE.fullmatch(value):
      ident,version=_ref(value); source=need(k,ident,m,f)
      if source and (source.data.get("version")!=version or source.data.get("license_status")!="approved" or (k=="character-spec" and source.data.get("review_status")!="approved")):out.append(_d(m,"SOURCE_NOT_APPROVED",f"source {k} is not approved",f))
 return out

def validate_path(path:Path)->list[Diagnostic]:
 docs,errors=load_path(path); errors.extend(validate_set(docs)); return errors
