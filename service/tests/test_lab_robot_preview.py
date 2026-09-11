"""2026-09-11 用户反馈：动作页「模拟动作」的上下反了；右侧「左右 / 上下」滑杆应该只动头，身子不能动。

真机约定：逻辑 y 越大越抬头（预设 look_up y=110、look_down y=70），x 越大是机器人自己的左边（look_left x=150）。
three.js 里 headPivot 绕 X 轴正转是低头，所以俯仰必须取反；绕 Y 轴正转脸朝观众右边，与 x 方向一致不用改。
滑杆原来绑的是 turntable（整个身子的观察视角），现在直接绑 simServo.x / simServo.y，范围来自舵机限位。
"""

from __future__ import annotations

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "src/deskbot_server/web/templates/app2c"


def test_lab_preview_pitch_matches_real_head():
    lab = (TEMPLATES / "lab.html").read_text(encoding="utf-8")
    assert "st.targetHeadPitch=-THREE.MathUtils.degToRad(this.robotVertDeg);" in lab
    assert "st.targetHeadYaw=THREE.MathUtils.degToRad(this.robotHorizDeg);" in lab
    # 逻辑角仍是「相对居中 90 的偏移」：y=110 → +20°，取反后才是抬头
    assert "return y - ROBOT_SERVO_CENTER;" in lab


def test_home_preview_pitch_matches_lab():
    home = (TEMPLATES / "home.html").read_text(encoding="utf-8")
    assert "state.targetHeadPitch = -THREE.MathUtils.degToRad(this.homeRobotPitchDeg);" in home
    assert "state.targetHeadYaw = THREE.MathUtils.degToRad(this.homeRobotYawDeg);" in home


def test_lab_sliders_move_only_the_head():
    lab = (TEMPLATES / "lab.html").read_text(encoding="utf-8")
    assert 'v-model.number="simServo.x"' in lab and 'v-model.number="simServo.y"' in lab
    assert ':min="simRange.xMin" :max="simRange.xMax"' in lab and ':min="simRange.yMin" :max="simRange.yMax"' in lab
    # 转身子的观察视角整套删掉：数据、标签、watch、同步函数、turntable 旋转
    for gone in ("robotViewYawDeg", "robotViewPitchDeg", "robotViewYawLabel", "_robotSyncView", "turntable.rotation", "<span>视角</span>"):
        assert gone not in lab, gone
    # 滑杆范围 = 用户限位优先、其次硬件包络；契约没到就禁用
    assert "pick(c.x_min,env.xMin,0)" in lab and "pick(c.y_max,env.yMax,180)" in lab
    assert lab.count(':disabled="!contract.ready" aria-label="') == 2
