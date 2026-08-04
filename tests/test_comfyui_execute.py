from __future__ import annotations

import json
import os
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from ai_illustration.adapters.base import AdapterError
from ai_illustration.adapters.comfyui_execute import (
    MANIFEST_FILE,
    check_comfyui_execution,
    prepare_execution,
    run_comfyui_execution,
)
from ai_illustration.frame_renderer import RGBAImage, encode_rgba_png
from ai_illustration.naming import canonical_json, content_identifier


def canonical(value):
    return canonical_json(value) + b"\n"


class ServerState:
    def __init__(self, png: bytes):
        self.png = png
        self.posts = 0
        self.post_payloads = []
        self.history_calls = 0
        self.mode = "success"


class Handler(BaseHTTPRequestHandler):
    server_version = "test"
    def log_message(self, *args): pass
    @property
    def state(self): return self.server.state
    def _send(self, code, content_type, payload, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        if extra:
            for key,value in extra.items(): self.send_header(key,value)
        self.end_headers(); self.wfile.write(payload)
    def do_POST(self):
        if self.path != "/prompt": self._send(404,"application/json",b"{}"); return
        if self.state.mode == "redirect": self.send_response(302); self.send_header("Location","http://127.0.0.1:1/evil"); self.end_headers(); return
        length=int(self.headers.get("Content-Length","0")); body=self.rfile.read(length)
        self.state.posts += 1; self.state.post_payloads.append(body)
        if self.state.mode == "duplicate_queue": self._send(200,"application/json",b'{"prompt_id":"a","prompt_id":"b"}'); return
        if self.state.mode == "queue_unknown": self._send(200,"application/json",b'{"prompt_id":"prompt-1","evil":true}'); return
        self._send(200,"application/json",b'{"node_errors":{},"number":1,"prompt_id":"prompt-1"}')
    def do_GET(self):
        parsed=urlsplit(self.path)
        if parsed.path == "/history/prompt-1":
            self.state.history_calls += 1
            if self.state.mode == "pending": self._send(200,"application/json",b"{}"); return
            if self.state.mode == "wrong_prompt": self._send(200,"application/json",b'{"other":{}}'); return
            if self.state.mode == "execution_error":
                value={"prompt-1":{"status":{"status_str":"error"}}}; self._send(200,"application/json",json.dumps(value).encode()); return
            if self.state.mode == "unsafe_name":
                value={"prompt-1":{"outputs":{"9":{"images":[{"filename":"../evil.png","subfolder":"","type":"output"}]}}}}
            elif self.state.mode == "wrong_type":
                value={"prompt-1":{"outputs":{"9":{"images":[{"filename":"x.png","subfolder":"","type":"temp"}]}}}}
            elif self.state.mode == "too_many":
                value={"prompt-1":{"outputs":{"9":{"images":[{"filename":"a.png","subfolder":"","type":"output"},{"filename":"b.png","subfolder":"","type":"output"}]}}}}
            elif self.state.mode == "wrong_node":
                value={"prompt-1":{"outputs":{"10":{"images":[{"filename":"x.png","subfolder":"","type":"output"}]}}}}
            else:
                value={"prompt-1":{"outputs":{"9":{"images":[{"filename":"x.png","subfolder":"batch","type":"output"}]}}}}
            self._send(200,"application/json",json.dumps(value,separators=(",",":"),sort_keys=True).encode()); return
        if parsed.path == "/view":
            query=parse_qs(parsed.query)
            if query.get("type") != ["output"]: self._send(400,"application/json",b"{}"); return
            self._send(200,"image/png",self.state.png); return
        self._send(404,"application/json",b"{}")


class Fixture:
    def __init__(self, root: Path):
        self.root=root
        self.request=root/"request.json"; self.workflow=root/"workflow.json"; self.bindings=root/"bindings.json"
        self.tool=root/"tool.json"; self.model=root/"model.json"; self.execution=root/"execution.json"
        request={"id":"request-demo","kind":"generation-request","schema_version":"1.0","character_ref":"character-demo@v001","style_ref":"style-demo@v001","pose":"standing","expression":"neutral","crop":"full-body","facing":"front","tool_id":"tool-approved","model_id":"model-approved","seed":7,"license_status":"approved","config":{"steps":1},"output_intent":"evaluation","provenance":{"source":"fixture"}}
        workflow={"1":{"class_type":"KSampler","inputs":{"seed":0,"steps":1}},"9":{"class_type":"SaveImage","inputs":{"images":["1",0]}}}
        bindings={"seed":{"node_id":"1","input":"seed","source":"seed"},"steps":{"node_id":"1","input":"steps","source":"config.steps"}}
        def profile(pid,ptype): return {"kind":"tool-profile","schema_version":"1.0","id":pid,"version":"v001","profile_type":ptype,"adapter_type":"comfyui-local-api","runtime_type":"python","offline_capability":"yes","deterministic_seed_support":True,"control_capabilities":["seed","workflow"],"minimum_vram_gb":0,"minimum_ram_gb":0,"supported_operating_systems":["linux"],"install_state":"installed","evidence_references":[{"source_url":"https://example.invalid/evidence","retrieved_at":"2026-08-04","claim":"fixture"}],"license_evidence_state":"approved","commercial_use_review_state":"approved","decision_state":"approved"}
        self.request.write_bytes(canonical(request)); self.workflow.write_text(json.dumps(workflow,indent=2),encoding="utf-8"); self.bindings.write_text(json.dumps(bindings,indent=2),encoding="utf-8")
        self.tool.write_bytes(canonical(profile("tool-approved","tool"))); self.model.write_bytes(canonical(profile("model-approved","model-configuration")))
        import hashlib
        limits={"max_images":2,"max_queue_response_bytes":4096,"max_history_response_bytes":65536,"max_png_bytes":1048576,"max_total_png_bytes":2097152,"request_timeout_seconds":5,"poll_interval_ms":50,"overall_timeout_seconds":3}
        core={"kind":"comfyui-execution-profile","schema_version":"1.0","workflow_sha256":hashlib.sha256(self.workflow.read_bytes()).hexdigest(),"tool_profile_ref":"tool-approved","model_profile_ref":"model-approved","output_node_ids":["9"],"expected_width":2,"expected_height":2,"limits":limits}
        execution={"id":content_identifier("comfyui-execution-profile",core,20),**core}; self.execution.write_bytes(canonical(execution))


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name); self.fixture=Fixture(self.root)
        png=encode_rgba_png(RGBAImage(2,2,bytes([255,0,0,255]*4)))
        self.state=ServerState(png); self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler); self.server.state=self.state
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.endpoint=f"http://127.0.0.1:{self.server.server_address[1]}"; self.output=self.root/"out"
    def tearDown(self): self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()
    def execute(self, **kwargs):
        return run_comfyui_execution(self.fixture.request,self.fixture.workflow,self.fixture.bindings,self.fixture.tool,self.fixture.model,self.fixture.execution,self.output<H@‘5¤; model["ision_state"]="reviewing"; self.fixture.model.write_bytes(canonical(model))
        with self.assertRaises(AdapterError) as c: self.execute()
        self.assertEqual(c.exception.code,"PROFILE_APPROVAL")

    def test_malformed_approved_profile_is_rejected(self):
        tool=json.loads(self.fixture.tool.read_text()); tool["version"]="v1"; self.fixture.tool.write_bytes(canonical(tool))
        with self.assertRaises(AdapterError) as caught: self.execute()
        self.assertEqual(caught.exception.code,"PROFILE_VALIDATION")
        self.assertEqual(self.state.posts,0)

    def test_incomplete_generation_request_is_rejected(self):
        request=json.loads(self.fixture.request.read_text()); request.pop("character_ref"); self.fixture.request.write_bytes(canonical(request))
        with self.assertRaises(AdapterError) as caught: self.execute()
        self.assertEqual(caught.exception.code,"REQUEST_VALIDATION")
        self.assertEqual(self.state.posts,0)

    def test_nonloopback_and_secret_rejected_before_network(self):
        with self.assertRaises(AdapterError) as caught:
            prepare_execution(self.fixture.request,self.fixture.workflow,self.fixture.bindings,self.fixture.tool,self.fixture.model,self.fixture.execution,endpoint="http://192.168.1.1:8188")
        self.assertEqual(caught.exception.code,"UNSAFE_ENDPOINT")
        request=json.loads(self.fixture.request.read_text()); request["api_token"]="secret"; self.fixture.request.write_bytes(canonical(request))
        with self.assertRaises(AdapterError) as caught: self.execute()
        self.assertEqual(caught.exception.code,"SECRET_LIKE_DATA"); self.assertEqual(self.state.posts,0)

    def test_duplicate_and_unknown_queue_json_rejected(self):
        for mode,code in (("duplicate_queue","DUPLICATE_JSON_KEY"),("queue_unknown","QUEUE_RESPONSE_SCHEMA")):
            with self.subTest(mode=mode):
                self.state.mode=mode
                with self.assertRaises(AdapterError) as caught: self.execute()
                self.assertEqual(caught.exception.code,code)

    def test_execution_error_type_and_count_rejected(self):
        for mode,code in (("execution_error","EXECUTION_ERROR"),("wrong_type","OUTPUT_TYPE"),("too_many","IMAGE_COUNT")):
            with self.subTest(mode=mode):
                self.state.mode=mode
                if mode=="too_many":
                    profile=json.loads(self.fixture.execution.read_text()); profile["limits"]["max_images"]=1
                    core={k:v for k,v in profile.items() if k!="id"}; profile["id"]=content_identifier("comfyui-execution-profile",core,20); self.fixture.execution.write_bytes(canonical(profile))
                with self.assertRaises(AdapterError) as caught: self.execute()
                self.assertEqual(caught.exception.code,code)

    def test_missing_srgb_png_rejected(self):
        self.state.png=self.state.png.replace(b'sRGB',b'tEXt',1)
        with self.assertRaises(AdapterError) as caught: self.execute()
        self.assertEqual(caught.exception.code,"PNG_INVALID")

    def test_noncanonical_request_and_duplicate_source_json_rejected(self):
        data=json.loads(self.fixture.request.read_text()); self.fixture.request.write_text(json.dumps(data,indent=2),encoding="utf-8")
        with self.assertRaises(AdapterError) as caught: self.execute()
        self.assertEqual(caught.exception.code,"NONCANONICAL_JSON")
        self.fixture.request.write_text('{"id":"a","id":"b"}',encoding="utf-8")
        with self.assertRaises(AdapterError) as caught: self.execute()
        self.assertEqual(caught.exception.code,"DUPLICATE_JSON_KEY")

if __name__ == '__main__': unittest.main()
