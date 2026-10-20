import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.main import sentences, intent_queries, JobRequest, SceneInput

def test_sentence_split():
    assert sentences("Save money. Track spending! Plan ahead?") == ["Save money.", "Track spending!", "Plan ahead?"]

def test_concept_mapping_avoids_cloud_sky():
    q = intent_queries("Cloud computing makes data processing scalable.")
    assert any("server" in x or "data center" in x for x in q)

def test_abstract_text_has_safe_fallback_query():
    q = intent_queries("Trust is earned slowly.")
    assert q and all(isinstance(x, str) for x in q)

def test_request_requires_input():
    try: JobRequest()
    except Exception: return
    assert False, "Empty request should be rejected"

def test_scenes_request():
    req = JobRequest(scenes=[SceneInput(text="A student studies online", visual_hint="laptop in library")])
    assert req.scenes[0].text.startswith("A student")
