"""卡通（位图）表情：场景级 JPEG 资产 → PB 二进制附件 → 预览/库/生成接口契约。"""
from __future__ import annotations

import base64
import io
import json

import pytest


def _jpeg_b64(w: int = 240, h: int = 240, color=(0, 0, 0)) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _cartoon_scene(n: int = 3) -> dict:
    from deskbot_server.ark_image_gen import frames_to_scene

    return frames_to_scene([_jpeg_b64(color=(i * 40, 0, 0)) for i in range(n)], name="cartoon_x", title="卡通")


def test_scene_assets_are_validated_and_kept():
    from deskbot_server.face_expr_scenes_store import normalize_design_scene

    scene = normalize_design_scene(_cartoon_scene())
    assert len(scene["assets"]) == 3
    assert scene["frames"][1]["elements"]["extra"][0]["asset"] == 1
    with pytest.raises(ValueError):
        normalize_design_scene({**_cartoon_scene(), "assets": ["bm90IGEganBlZw=="]})  # not a JPEG


def test_pb_chain_attaches_assets_and_remaps_indices():
    from deskbot_server.face_expr_scenes_store import (
        decode_scene_assets,
        design_frames_to_pb_chain,
        normalize_design_scene,
    )

    scene = normalize_design_scene(_cartoon_scene())
    pairs = design_frames_to_pb_chain(scene["frames"], runtime_req="t", assets=decode_scene_assets(scene))
    assert pairs, "expected at least one PB message"
    # 三帧 450ms 会合并进同一条消息：3 个资产按引用顺序挂上，图元 asset 重编号为行内下标
    msg, bins = pairs[0]
    assert len(bins) == 3 and all(b.startswith(b"\xff\xd8\xff") for b in bins)
    assert [a["next_bin_len"] for a in msg["assets"]] == [len(b) for b in bins]
    refs = [p["asset"] for item in msg["anim"] for p in item["elements"]["extra"] if p["shape"] == "image"]
    assert refs == [0, 1, 2]


def test_transaction_accepts_image_primitives_with_assets():
    from deskbot_server.face_expression_transactions import _normalize_scene

    scene = _normalize_scene(_cartoon_scene(), name="cartoon_x", origin="user")
    assert scene["assets"] and scene["frames"][0]["elements"]["extra"][0]["shape"] == "image"
    bad = _cartoon_scene()
    bad["frames"][0]["elements"]["extra"][0]["asset"] = 9
    with pytest.raises(ValueError):
        _normalize_scene(bad, name="cartoon_x", origin="user")


def test_catalog_exposes_binaries_only_when_asked():
    from deskbot_server.application.expression_catalog import (
        ExpressionScene,
        build_expression_pb_frames,
    )
    from deskbot_server.face_expr_scenes_store import decode_scene_assets, normalize_design_scene

    raw = normalize_design_scene(_cartoon_scene())
    scene = ExpressionScene(name="cartoon_x", title="卡通", aliases=(), frames=tuple(raw["frames"]), assets=tuple(decode_scene_assets(raw)))
    with pytest.raises(ValueError):
        build_expression_pb_frames(scene, request_id="r1")
    out: list[list[bytes]] = []
    messages = build_expression_pb_frames(scene, request_id="r1", out_binaries=out)
    assert len(out) == len(messages) and sum(len(b) for b in out) == 3


def test_generate_cartoon_frames_uses_style_prompt_and_canonicalizes(monkeypatch):
    from deskbot_server.ark_image_gen import generate_cartoon_frames

    captured = {}

    def fake_transport(url, payload, api_key, timeout):
        captured["payload"] = payload
        big = _jpeg_b64(1024, 1024, (0, 255, 255))
        return {"data": [{"b64_json": big} for _ in range(3)], "usage": {"generated_images": 3}}

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    result = generate_cartoon_frames("坏笑", style="lineart", transport=fake_transport)
    p = captured["payload"]
    assert p["model"].startswith("doubao-seedream")
    assert p["sequential_image_generation_options"] == {"max_images": 3}
    assert "线稿" in p["prompt"] and "坏笑" in p["prompt"] and "纯黑色 #000000" in p["prompt"]
    # 设备只有 240²：默认 Seedream 4.0 用它允许的最小尺寸 1024²（5.0 最小 1920²，慢一倍多）
    assert p["size"] == "1024x1024"
    assert len(result["frames"]) == 3
    from deskbot_server.pb.llm_display import jpeg_blob_dimensions

    assert jpeg_blob_dimensions(base64.b64decode(result["frames"][0])) == (240, 240)


def test_default_cartoon_set_file_is_valid():
    """默认套图 = 待机/倾听/思考/说话四个状态，每个都是可入库的位图场景（3 帧 + 3 张 JPEG）。"""
    from pathlib import Path

    from deskbot_server.face_expression_transactions import _normalize_scene

    path = Path(__file__).resolve().parents[1] / "src/deskbot_server/web/static/cartoon_default_set.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    states = doc["states"]
    assert set(states) == {"idle", "listening", "thinking", "speaking"}
    for key, scene in states.items():
        normalized = _normalize_scene({**scene, "name": f"cartoon_{key}"}, name=f"cartoon_{key}", origin="user")
        assert len(normalized["assets"]) == 3 and len(normalized["frames"]) == 3



def test_bitmap_scene_is_filled_to_a_looping_timeline():
    """固件对 PB 只播一遍：位图表情在没有显式 duration 时把帧周期铺满 ≈9s（单条消息、附件只带一次），
    配合运行时 bitmap_loop 续发形成循环；有显式 duration 时不铺满。"""
    from deskbot_server.application.expression_catalog import (
        BITMAP_LOOP_FILL_MS,
        ExpressionScene,
        build_expression_pb_frames,
    )
    from deskbot_server.face_expr_scenes_store import decode_scene_assets, normalize_design_scene

    raw = normalize_design_scene(_cartoon_scene())
    scene = ExpressionScene(name="cartoon_x", title="卡通", aliases=(), frames=tuple(raw["frames"]), assets=tuple(decode_scene_assets(raw)))
    out: list[list[bytes]] = []
    messages = build_expression_pb_frames(scene, request_id="r1", out_binaries=out)
    total_ms = sum(int(m.get("chunk_ms") or 0) for m in messages)
    assert total_ms >= BITMAP_LOOP_FILL_MS and len(messages) == 1
    assert len(out[0]) == 3  # 附件只带一次
    assert len(messages[0]["anim"]) >= 3 * 6
    short = build_expression_pb_frames(scene, request_id="r2", duration_ms=1350, out_binaries=[])
    assert len(short[0]["anim"]) == 3


def test_bitmap_scene_never_uses_voice_mouth_overlay():
    """固件在新 PB 到来时丢掉上一帧的 JPEG 图元，位图脸上叠口型会变成黑脸一张嘴：
    位图场景构建时必须强制 voice_mouth=False（说话状态改用说话动画循环）。"""
    from deskbot_server.application.expression_catalog import (
        ExpressionScene,
        build_expression_pb_frames,
    )
    from deskbot_server.face_expr_scenes_store import decode_scene_assets, normalize_design_scene

    raw = normalize_design_scene(_cartoon_scene())
    scene = ExpressionScene(name="cartoon_x", title="卡通", aliases=(), frames=tuple(raw["frames"]), assets=tuple(decode_scene_assets(raw)))
    messages = build_expression_pb_frames(scene, request_id="r1", voice_mouth=True, out_binaries=[])
    assert all(not m.get("voice_mouth") for m in messages)


def test_state_entry_frames_keep_bitmap_assets():
    """状态推送会用 _state_entry_frames 截取入场帧并构造新的 ExpressionScene；曾丢掉 assets，
    导致倾听/思考等映射到卡通表情时设备收到"有 image 图元、无附件"的 PB 而拒帧。"""
    from deskbot_server.application.expression_catalog import ExpressionScene, _state_entry_frames
    from deskbot_server.face_expr_scenes_store import decode_scene_assets, normalize_design_scene

    raw = normalize_design_scene(_cartoon_scene())
    scene = ExpressionScene(name="cartoon_x", title="卡通", aliases=(), frames=tuple(raw["frames"]), assets=tuple(decode_scene_assets(raw)))
    for state in ("listening", "thinking", "speaking", "idle"):
        entry = _state_entry_frames(scene, state)
        assert entry.assets == scene.assets, state


def test_blacken_background_only_touches_near_black_pixels():
    """Seedream 常把"纯黑背景"画成深灰，设备真黑屏上会显出灰方框：生成帧先把阈值以下压成纯黑，
    高亮的五官像素必须原样保留。"""
    from PIL import Image, ImageDraw

    from deskbot_server.ark_image_gen import blacken_background

    img = Image.new("RGB", (4, 1), (5, 5, 3))
    img.putpixel((1, 0), (255, 255, 255))
    img.putpixel((2, 0), (255, 120, 160))
    img.putpixel((3, 0), (10, 12, 25))  # kawaii 细眉毛：蓝分量必须保留
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out = Image.open(io.BytesIO(blacken_background(buf.getvalue()))).convert("RGB")
    assert out.getpixel((0, 0)) == (0, 0, 0)
    assert out.getpixel((1, 0)) == (255, 255, 255)
    assert out.getpixel((2, 0)) == (255, 120, 160)
    assert out.getpixel((3, 0))[2] == 25

    # 白底 + 黑色圆角底板：四角亮 → 从角泛洪填黑；中间的白色五官不和角连通，保留。
    plate = Image.new("RGB", (40, 40), (250, 250, 250))
    d = ImageDraw.Draw(plate)
    d.rounded_rectangle((3, 3, 36, 36), radius=6, fill=(0, 0, 0))
    d.ellipse((15, 15, 24, 24), fill=(255, 255, 255))
    buf = io.BytesIO()
    plate.save(buf, format="PNG")
    out = Image.open(io.BytesIO(blacken_background(buf.getvalue()))).convert("RGB")
    assert out.getpixel((0, 0)) == (0, 0, 0)
    assert out.getpixel((39, 39)) == (0, 0, 0)
    assert out.getpixel((20, 20)) == (255, 255, 255)


def test_cartoon_prompt_keeps_explicit_frame_plan_and_styles_differ():
    """描述里已经写了逐帧分工时不再追加默认"眨眼"分工；单张预览不追加分工；
    演法按状态单独取用（把所有情绪都列进 prompt 会被画成"好几个头"）；四种风格的画法/演法互不相同；
    预览 variant 让三张彼此不同。"""
    from deskbot_server.ark_image_gen import (
        _DEFAULT_FRAME_PLAN,
        CARTOON_STYLES,
        build_cartoon_prompt,
    )

    explicit = build_cartoon_prompt("kawaii", "思考中：第一帧 1 个点、第二帧 2 个点、第三帧 3 个点", frames=3)
    assert _DEFAULT_FRAME_PLAN not in explicit
    assert _DEFAULT_FRAME_PLAN in build_cartoon_prompt("kawaii", "开心的微笑", frames=3)
    single = build_cartoon_prompt("kawaii", "开心的微笑", frames=1)
    assert _DEFAULT_FRAME_PLAN not in single and "只输出 1 张图" in single and "只有一张脸" in single

    thinking = build_cartoon_prompt("lineart", "x", frames=3, state="thinking")
    assert CARTOON_STYLES["lineart"]["acting"]["thinking"] in thinking
    assert CARTOON_STYLES["lineart"]["acting"]["happy"] not in thinking
    assert all(len(val["acting"]) >= 9 for val in CARTOON_STYLES.values())
    assert len({val["prompt"] for val in CARTOON_STYLES.values()}) == len(CARTOON_STYLES)
    assert len({val["acting"]["thinking"] for val in CARTOON_STYLES.values()}) == len(CARTOON_STYLES)
    variants = {build_cartoon_prompt("kawaii", "x", frames=1, variant=i) for i in range(3)}
    assert len(variants) == 3


def test_styles_own_their_background_and_only_black_styles_get_blackened(monkeypatch):
    """不是所有风格都是黑底（用户 2026-09-07）：背景是风格属性写进 prompt；米白纸底/纯色底的风格
    不能再走"近黑压纯黑 + 四角泛洪"，否则整张底色被吃掉。"""
    from PIL import Image

    from deskbot_server.ark_image_gen import (
        CARTOON_STYLES,
        build_cartoon_prompt,
        generate_cartoon_frames,
    )
    from deskbot_server.pb.llm_display import jpeg_blob_dimensions

    assert "纯黑色 #000000" in build_cartoon_prompt("kawaii", "x", frames=1)
    assert CARTOON_STYLES["doodle"]["background"] in build_cartoon_prompt("doodle", "x", frames=1)
    assert len({val["background"] for val in CARTOON_STYLES.values()}) >= 3

    def fake_transport(url, payload, api_key, timeout):
        return {"data": [{"b64_json": _jpeg_b64(1024, 1024, (243, 235, 216))}]}

    monkeypatch.setenv("LLM_API_KEY", "test-key")
    doodle = generate_cartoon_frames("x", style="doodle", frames=1, transport=fake_transport)
    img = Image.open(io.BytesIO(base64.b64decode(doodle["frames"][0]))).convert("RGB")
    r, g, b = img.getpixel((3, 3))
    assert r > 200 and g > 200 and b > 180, "纸底风格不能被压成黑色"
    kawaii = generate_cartoon_frames("x", style="kawaii", frames=1, transport=fake_transport)
    img = Image.open(io.BytesIO(base64.b64decode(kawaii["frames"][0]))).convert("RGB")
    assert img.getpixel((3, 3)) == (0, 0, 0), "黑底风格的亮底从四角泛洪填黑"
    assert jpeg_blob_dimensions(base64.b64decode(kawaii["frames"][0])) == (240, 240)


def test_cartoon_generation_has_its_own_daily_quota(tmp_path, monkeypatch):
    """一轮套图 = 3 张预览 + 4 个状态，共用设置测试 50 次/天几轮就 429；生图单独按张记账。"""
    from deskbot_server.application.cartoon_gen_limit import (
        CartoonGenLimitExceeded,
        check_and_consume_cartoon_gen,
    )

    monkeypatch.setenv("DESKBOT_CARTOON_GEN_DAILY_LIMIT", "5")
    assert check_and_consume_cartoon_gen(images=3, root=tmp_path).remaining == 2
    assert check_and_consume_cartoon_gen(images=2, root=tmp_path).remaining == 0
    with pytest.raises(CartoonGenLimitExceeded):
        check_and_consume_cartoon_gen(images=1, root=tmp_path)


def test_builtin_cartoon_default_set_is_system_scene_not_user_library():
    """内置默认套图注册为 system 场景 cartoon_default_*：可被映射/下发（带 assets），
    但 origin 不是 user，不会出现在「我的表情」；只有新生成的套图才入库（用户拍板 2026-09-07）。"""
    from deskbot_server.face_design_store import emotions_as_scenes
    from deskbot_server.face_expr_scenes_store import (
        builtin_emotion_scenes,
        cartoon_default_scene_name,
    )

    names = {row["name"] for row in builtin_emotion_scenes()}
    for state in ("idle", "listening", "thinking", "speaking"):
        assert cartoon_default_scene_name(state) in names
    rows = emotions_as_scenes({"emotions": [], "phonemes": [], "mappings": {}})
    cartoon = [r for r in rows if r["name"].startswith("cartoon_default_")]
    assert len(cartoon) == 4
    assert all(r["origin"] == "system" and len(r.get("assets") or []) == 3 and len(r["frames"]) == 3 for r in cartoon)


def test_audio_pb_skips_mouth_animation_when_face_is_bitmap():
    """屏幕上是卡通（位图）脸时，TTS 音频分片不带显示层动画：固件按"有无 anim"占用显示通道，
    带口型会把 JPEG 脸丢掉只剩一张嘴（用户 2026-09-07 实测），说话动画交给卡通"说话"循环。"""
    from deskbot_server.pb.wire import build_pb_wire_pairs

    segs = [{"phoneme": "a", "ms": 200, "pcm": b"\x00\x00" * 4800}, {"phoneme": "o", "ms": 200, "pcm": b"\x00\x00" * 4800}]
    pairs, _req, n_pb, _sr = build_pb_wire_pairs(segs, {}, sample_rate=24000, mouth_only=True, display_anim=False)
    assert n_pb >= 1
    for msg, binaries in pairs:
        assert "anim" not in msg and "mouth_only" not in msg
        assert int(msg.get("chunk_ms") or 0) > 0
        assert binaries and binaries[0], "音频仍然要发"


def test_runtime_catalog_resolves_builtin_cartoon_scenes_with_assets():
    """Core 运行时目录必须包含内置卡通套图（映射到 cartoon_default_* 后倾听/说话才是卡通而不是矢量+口型）。"""
    from deskbot_server.application.expression_catalog import build_expression_catalog

    catalog = build_expression_catalog({"emotions": [], "phonemes": [], "mappings": {"listening": "cartoon_default_listening"}})
    scene = catalog.resolve_scene("cartoon_default_listening")
    assert scene is not None and len(scene.assets) == 3 and len(scene.frames) == 3


def test_audio_pb_with_head_moves_still_skips_mouth_animation_on_bitmap_face():
    """带 moves 的手动 TTS 走 LLM 共享时间线分支：位图脸下同样不能带口型，舵机步照常保留。"""
    from deskbot_server.pb.wire import build_pb_wire_pairs

    segs = [{"phoneme": "a", "ms": 300, "pcm": b"\x00\x00" * 7200}, {"phoneme": "o", "ms": 300, "pcm": b"\x00\x00" * 7200}]
    moves = [{"move": "nod_head", "ms": 600}]
    pairs, _req, _n, _sr = build_pb_wire_pairs(segs, {}, moves=moves, sample_rate=24000, mouth_only=True, display_anim=False)
    assert pairs
    assert all("anim" not in msg and "mouth_only" not in msg for msg, _b in pairs)
    assert any(msg.get("servo") for msg, _b in pairs), "舵机动作不能被一起剥掉"
