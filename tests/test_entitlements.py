"""签名用 entitlements 与 spec 里用途描述的一致性。

Hardened Runtime 下，受保护资源除了 Info.plist 的 NS*UsageDescription，
还必须有对应的 entitlement；缺了系统直接拒绝、不弹授权（在终端里跑
OpenCV 打不开摄像头即此因）。
"""

import plistlib
import re
from pathlib import Path

PROJ = Path(__file__).resolve().parent.parent

# 用途描述 → Hardened Runtime 必需的 entitlement
REQUIRED = {
    "NSCameraUsageDescription": "com.apple.security.device.camera",
    "NSMicrophoneUsageDescription": "com.apple.security.device.audio-input",
    "NSLocationUsageDescription": "com.apple.security.personal-information.location",
    "NSContactsUsageDescription": "com.apple.security.personal-information.addressbook",
    "NSCalendarsUsageDescription": "com.apple.security.personal-information.calendars",
    "NSPhotoLibraryUsageDescription": "com.apple.security.personal-information.photos-library",
    "NSAppleEventsUsageDescription": "com.apple.security.automation.apple-events",
}


def _entitlements():
    with open(PROJ / "entitlements.plist", "rb") as f:
        return plistlib.load(f)


def test_usage_descriptions_have_matching_entitlements():
    spec = (PROJ / "smart_terminal.spec").read_text(encoding="utf-8")
    declared = set(re.findall(r'"(NS\w+UsageDescription)"', spec))
    ent = _entitlements()
    missing = [
        f"{desc} -> {key}"
        for desc, key in REQUIRED.items()
        if desc in declared and ent.get(key) is not True
    ]
    assert not missing, f"entitlements.plist 缺少: {missing}"


def test_camera_and_microphone_entitlements_present():
    ent = _entitlements()
    assert ent.get("com.apple.security.device.camera") is True
    assert ent.get("com.apple.security.device.audio-input") is True
