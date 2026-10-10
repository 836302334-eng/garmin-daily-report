# -*- coding: utf-8 -*-
"""
Garmin 训练日报 · GitHub Actions 版
每天北京时间 10:00 自动运行，生成当天日报并推送到企业微信。
"""

import datetime
import json
import logging
import os
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from garminconnect import Garmin

# ==================== 配置区 ====================
CN_TZ = ZoneInfo("Asia/Shanghai")

EMAIL = os.environ.get("GARMIN_USER", "").strip()
PASSWORD = os.environ.get("GARMIN_PASS", "").strip()
WECOM_WEBHOOK = os.environ.get("WECOM_WEBHOOK", "").strip()

TOKEN_DIR = Path(os.environ.get("GARMIN_TOKEN_DIR", "~/.garminconnect")).expanduser()
REPORT_DIR = Path(os.environ.get("REPORT_DIR", "reports")).expanduser()
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
DATE_OFFSET = int(os.environ.get("DATE_OFFSET", "0"))  # 0=当天，1=前一天
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("garmin")


# ==================== 通用工具 ====================
def safe(fn):
    try:
        return fn()
    except Exception:
        return None


def login_garmin():
    if not EMAIL or not PASSWORD:
        log.error("缺少 GARMIN_USER / GARMIN_PASS 环境变量")
        sys.exit(2)

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    api = Garmin(EMAIL, PASSWORD, is_cn=True, verify_login=False)

    try:
        api.login(str(TOKEN_DIR))
        log.info("Garmin 登录成功（token 缓存）")
    except Exception as e:
        log.warning("token 缓存失效，改用账号密码：%s", e)
        api.login()
        try:
            api.garth.dump(str(TOKEN_DIR))
            log.info("已刷新 token 缓存 → %s", TOKEN_DIR)
        except Exception as e2:
            log.warning("token 保存失败（不影响主流程）：%s", e2)
    return api


def push_wecom(content: str, webhook: str = None, msgtype: str = "markdown"):
    """企业微信机器人推送。失败只记日志，不抛异常。"""
    webhook = webhook or WECOM_WEBHOOK
    if not webhook:
        log.info("未配置 WECOM_WEBHOOK，跳过推送")
        return False

    try:
        r = requests.post(
            webhook,
            json={"msgtype": msgtype, msgtype: {"content": content}},
            timeout=10,
        )
        ok = r.status_code == 200 and r.json().get("errcode") == 0
        if ok:
            log.info("企业微信推送成功")
        else:
            log.warning("企业微信推送失败：%s %s", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        log.warning("企业微信推送异常：%s", e)
        return False


def build_push_summary(p: dict, issues: list, report_date: str, file_path: Path) -> str:
    """生成企业微信推送摘要。"""
    ra = p.get("run_activity") or {}
    lines = [f"**Garmin 训练日报 · {report_date}**", ""]

    if ra:
        lines.append(
            f"🏃 {ra.get('distance_km')}km / {ra.get('duration')} "
            f"/ 配速 {ra.get('pace')}/km / 心率 {ra.get('avg_hr')}"
        )
    else:
        lines.append("🏃 前 24h 无跑步")

    lines.append(
        f"😴 睡眠 {p.get('sleep_score')}({p.get('sleep_qualifier')}) "
        f"{p.get('sleep_duration')}｜HRV {p.get('hrv_status')} {p.get('hrv_night')}ms"
    )
    lines.append(
        f"❤️ 静息 {p.get('rhr')}｜电量 {p.get('body_battery')}({p.get('bb_level')})"
    )

    if issues:
        lines.append("")
        for name, fact, sev, advice in issues:
            icon = "🔴" if sev == "红" else "🟡"
            lines.append(f"{icon} {name}｜{advice}")
    else:
        lines.append("✅ 无异常")

    lines.append("")
    lines.append(f"📄 完整日报：`{file_path.name}`")

    text = "\n".join(lines)
    b = text.encode("utf-8")
    if len(b) > 3800:
        text = b[:3800].decode("utf-8", errors="ignore") + "\n…（已截断）"
    return text


def cleanup_old_reports(report_dir: Path, retention_days: int):
    """删除超过 retention_days 的日报文件。"""
    if retention_days <= 0:
        return

    cutoff = datetime.datetime.now(CN_TZ).date() - datetime.timedelta(days=retention_days)
    pattern = re.compile(r"^日报_(\d{4}-\d{2}-\d{2})\.txt$")
    removed = 0

    for f in report_dir.glob("日报_*.txt"):
        m = pattern.match(f.name)
        if not m:
            continue
        try:
            d = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if d < cutoff:
            try:
                f.unlink()
                removed += 1
            except OSError as e:
                log.warning("删除失败 %s：%s", f, e)

    if removed:
        log.info("已清理 %d 个过期日报（>%d 天）", removed, retention_days)


# ==================== 拉数据 ====================
def fetch_all(api, date):
    data = {}
    data["training_status"] = safe(lambda: api.get_training_status(date))
    data["sleep"] = safe(lambda: api.get_sleep_data(date))
    data["hrv"] = safe(lambda: api.get_hrv_data(date))
    data["summary"] = safe(lambda: api.get_user_summary(date))
    data["body_battery"] = safe(lambda: api.get_body_battery(date))
    data["next_workout"] = safe(lambda: api.get_next_scheduled_workout())
    data["activities"] = safe(lambda: api.get_activities(0, 20))
    return data


# ==================== 解析区 ====================
TS_MAP = {0: "无", 1: "退化停训", 2: "过度训练", 3: "无成效",
          4: "恢复中", 5: "维持", 6: "巅峰", 7: "富有成效"}
FIT_TREND = {1: "下降", 2: "持平", 3: "提升"}
ACWR_STATUS = {"OPTIMAL": "最佳", "MODERATE": "偏高", "HIGH": "过高", "LOW": "偏低"}
QUAL = {"EXCELLENT": "优秀", "GOOD": "良好", "FAIR": "一般", "POOR": "差"}
HRV_STATUS = {"BALANCED": "平衡", "UNBALANCED": "失衡", "LOW": "偏低", "POOR": "很差"}
SPLIT_TYPE = {"INTERVAL_WARMUP": "热身", "INTERVAL_ACTIVE": "间歇", "INTERVAL_COOLDOWN": "冷身",
              "INTERVAL_REST": "组间休息", "RWD_RUN": "跑步", "RWD_WALK": "步行"}
TE_MAP = {"AEROBIC_BASE": "有氧基础", "ANAEROBIC": "无氧", "VO2MAX": "VO2Max",
          "TEMPO": "节奏跑", "THRESHOLD": "阈值跑", "LACTATE_THRESHOLD": "乳酸阈值",
          "RECOVERY": "恢复", "INVALID": "-"}


def fmt_sec(sec):
    if sec is None:
        return "-"
    sec = int(sec)
    h, m = divmod(sec, 3600)
    m //= 60
    return f"{h}小时{m}分" if h else f"{m}分钟"


def fmt_pace(sec_per_km):
    if not sec_per_km:
        return "-"
    m = int(sec_per_km) // 60
    s = int(sec_per_km) % 60
    return f"{m}'{s:02d}\""


def hours_ago(activity):
    bt = activity.get("beginTimestamp")
    if not bt:
        return None
    t = datetime.datetime.fromtimestamp(bt / 1000, tz=datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    return round((now - t).total_seconds() / 3600, 1)


def parse_run(a, hr_zones):
    d = a.get("distance") or 0
    dur = a.get("duration") or 0
    pace = (dur / (d / 1000)) if d else None

    best = None
    if a.get("maxSpeed"):
        best = 1000 / a["maxSpeed"]

    splits = []
    for s in a.get("splitSummaries", []):
        sd = s.get("distance") or 0
        sdur = s.get("duration") or 0
        sp = (sdur / (sd / 1000)) if sd else None
        splits.append({
            "type": SPLIT_TYPE.get(s.get("splitType"), s.get("splitType")),
            "distance_km": round(sd / 1000, 2),
            "duration": fmt_sec(sdur),
            "pace": fmt_pace(sp),
        })

    zones = []
    if hr_zones:
        total = sum(z.get("secsInZone", 0) for z in hr_zones) or 1
        for z in hr_zones:
            sec = z.get("secsInZone", 0)
            zones.append({"zone": z.get("zoneNumber"), "sec": int(sec),
                          "pct": round(sec / total * 100)})

    hour = None
    try:
        hour = int(a.get("startTimeLocal", "").split(" ")[1].split(":")[0])
    except Exception:
        hour = None
    period = "晨跑" if (hour is not None and 5 <= hour < 12) else ("夜跑" if hour is not None else "-")

    return {
        "name": a.get("activityName"),
        "time": a.get("startTimeLocal"),
        "period": period,
        "hours_ago": hours_ago(a),
        "temp_min": a.get("minTemperature"),
        "temp_max": a.get("maxTemperature"),
        "device": None,
        "distance_km": round(d / 1000, 2) if d else 0,
        "duration": fmt_sec(dur),
        "elapsed": fmt_sec(a.get("elapsedDuration")),
        "pace": fmt_pace(pace),
        "best_pace": fmt_pace(best),
        "splits": splits,
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "zones": zones,
        "avg_cadence": round(a["averageRunningCadenceInStepsPerMinute"]) if a.get("averageRunningCadenceInStepsPerMinute") else None,
        "max_cadence": round(a["maxRunningCadenceInStepsPerMinute"]) if a.get("maxRunningCadenceInStepsPerMinute") else None,
        "stride": round(a["avgStrideLength"] / 100, 2) if a.get("avgStrideLength") else None,
        "vo_osc": round(a["avgVerticalOscillation"], 1) if a.get("avgVerticalOscillation") else None,
        "gct": round(a["avgGroundContactTime"]) if a.get("avgGroundContactTime") else None,
        "vert_ratio": round(a["avgVerticalRatio"], 1) if a.get("avgVerticalRatio") else None,
        "aerobic_te": round(a["aerobicTrainingEffect"], 1) if a.get("aerobicTrainingEffect") is not None else None,
        "anaerobic_te": round(a["anaerobicTrainingEffect"], 1) if a.get("anaerobicTrainingEffect") is not None else None,
        "training_load": round(a["activityTrainingLoad"], 1) if a.get("activityTrainingLoad") else None,
        "calories": round(a["calories"]) if a.get("calories") else None,
        "sweat": round(a["waterEstimated"]) if a.get("waterEstimated") else None,
        "elevation_gain": round(a.get("elevationGain") or 0, 1),
    }


def parse_strength(a, exercise_sets):
    return {
        "name": a.get("activityName"),
        "time": a.get("startTimeLocal"),
        "hours_ago": hours_ago(a),
        "duration": fmt_sec(a.get("duration")),
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "calories": round(a["calories"]) if a.get("calories") else None,
        "set_count": len(exercise_sets or []),
    }


def analyze_intensity(api, activity_id):
    """识别跑步中的高强度段（冲刺/间歇/阈值）。"""
    detail = safe(lambda: api.get_activity_details(activity_id))
    if not detail:
        return None

    idx = {d.get("key"): d.get("metricsIndex") for d in detail.get("metricDescriptors", [])}

    def g(m, key):
        i = idx.get(key)
        return m[i] if (i is not None and i < len(m)) else None

    rows = []
    for m in detail.get("activityDetailMetrics", []):
        arr = m.get("metrics", [])
        rows.append({
            "t": g(arr, "sumElapsedDuration"),
            "speed": g(arr, "directSpeed"),
            "hr": g(arr, "directHeartRate"),
            "cad": g(arr, "directDoubleCadence"),
            "gct": g(arr, "directGroundContactTime"),
            "osc": g(arr, "directVerticalOscillation"),
            "stride": g(arr, "directStrideLength"),
        })

    mov = [r for r in rows if r["speed"] and r["speed"] > 0.5]
    if len(mov) < 10:
        return None

    avg_speed = sum(r["speed"] for r in mov) / len(mov)
    threshold = max(avg_speed * 1.2, 3.0)
    hi = [r for r in mov if r["speed"] >= threshold]
    if len(hi) < 5:
        return None

    def avg(vals):
        v = [x for x in vals if x is not None]
        return round(sum(v) / len(v), 1) if v else None

    def mx(vals):
        v = [x for x in vals if x is not None]
        return round(max(v), 1) if v else None

    segs = 0
    prev_hi = False
    for r in mov:
        is_hi = r["speed"] >= threshold
        if is_hi and not prev_hi:
            segs += 1
        prev_hi = is_hi

    t0 = mov[0]["t"]
    t1 = mov[-1]["t"]
    interval = (t1 - t0) / (len(mov) - 1) if (t0 is not None and t1 is not None and len(mov) > 1) else 1.0
    dur_sec = round(len(hi) * interval)

    cad_vals = [r["cad"] for r in hi if r["cad"] and r["cad"] > 100]
    gct_vals = [r["gct"] for r in hi if r["gct"]]
    cad_avg = avg(cad_vals)
    gct_avg = avg(gct_vals)

    air_avg = None
    gct_ratio = None
    if gct_avg and cad_avg:
        cycle_ms = 60000 / cad_avg
        air_avg = round(cycle_ms - gct_avg, 1)
        if air_avg > 0:
            gct_ratio = round(gct_avg / air_avg, 2)

    hr_recovery = None
    peak = None
    for r in hi:
        if r["hr"] and (peak is None or r["hr"] > peak["hr"]):
            peak = r
    if peak and peak["t"] is not None:
        after = [r["hr"] for r in rows if r["t"] and peak["t"] < r["t"] <= peak["t"] + 120 and r["hr"]]
        if after:
            hr_recovery = round(peak["hr"] - min(after), 1)

    return {
        "segments": segs,
        "threshold_pace": fmt_pace(1000 / threshold),
        "duration": fmt_sec(dur_sec),
        "hr_avg": avg([r["hr"] for r in hi]),
        "hr_max": mx([r["hr"] for r in hi]),
        "cad_avg": cad_avg,
        "cad_max": mx(cad_vals),
        "gct_avg": gct_avg,
        "gct_min": round(min(gct_vals), 1) if gct_vals else None,
        "air_avg": air_avg,
        "gct_ratio": gct_ratio,
        "osc_avg": avg([r["osc"] for r in hi]),
        "stride_avg": avg([r["stride"] for r in hi]),
        "hr_recovery": hr_recovery,
    }


def analyze_cardiac_drift(api, activity_id):
    """判断心率漂移。返回 '有'/'无'/'疑似有' 或 None。"""
    detail = safe(lambda: api.get_activity_details(activity_id))
    if not detail:
        return None

    idx = {d.get("key"): d.get("metricsIndex") for d in detail.get("metricDescriptors", [])}

    def g(m, key):
        i = idx.get(key)
        return m[i] if (i is not None and i < len(m)) else None

    rows = []
    for m in detail.get("activityDetailMetrics", []):
        arr = m.get("metrics", [])
        rows.append({
            "t": g(arr, "sumElapsedDuration"),
            "speed": g(arr, "directSpeed"),
            "hr": g(arr, "directHeartRate"),
        })

    mov = [r for r in rows if r["speed"] and r["speed"] > 0.5]
    if len(mov) < 30:
        return None

    avg = sum(r["speed"] for r in mov) / len(mov)
    hi_th = max(avg * 1.2, 3.0)
    steady = [r for r in mov if 2.0 <= r["speed"] < hi_th and r["hr"]]
    if len(steady) < 20:
        return None

    n = len(steady)
    front = steady[:n // 3]
    back = steady[-(n // 3):]

    def hravg(seg):
        v = [r["hr"] for r in seg if r["hr"]]
        return sum(v) / len(v) if v else None

    def spdavg(seg):
        s = [r["speed"] for r in seg if r["speed"]]
        return sum(s) / len(s) if s else None

    def tmin(seg):
        ts = [r["t"] for r in seg if r["t"] is not None]
        if not ts:
            return "-"
        return f"第{int(min(ts) / 60)}~{int(max(ts) / 60)}分钟"

    hf = hravg(front)
    hb = hravg(back)
    pf = spdavg(front)
    pb = spdavg(back)

    if hf is None or hb is None:
        return None

    drift = round(hb - hf, 1)
    accel = bool(pb and pf and pb > pf * 1.03)

    if accel:
        verdict = "无"
    elif drift >= 8:
        verdict = "有"
    elif drift >= 5:
        verdict = "疑似有"
    else:
        verdict = "无"

    return {
        "verdict": verdict,
        "drift": drift,
        "front_time": tmin(front),
        "back_time": tmin(back),
        "front_hr": round(hf),
        "back_hr": round(hb),
        "front_pace": fmt_pace(1000 / pf) if pf else None,
        "back_pace": fmt_pace(1000 / pb) if pb else None,
        "accel": accel,
    }


def parse(data, api=None, report_date=None):
    p = {}
    report_date = report_date or datetime.date.today().isoformat()
    today = datetime.date.fromisoformat(report_date)

    # --- 训练状态 ---
    ts = data["training_status"] or {}
    d = ts.get("mostRecentTrainingStatus", {}).get("latestTrainingStatusData", {})
    dev = list(d.values())[0] if d else {}
    p["training_status"] = TS_MAP.get(dev.get("trainingStatus"), "无")
    p["fitness_trend"] = FIT_TREND.get(dev.get("fitnessTrend"), "未知")
    acwr = dev.get("acuteTrainingLoadDTO") or {}
    p["acwr_ratio"] = acwr.get("dailyAcuteChronicWorkloadRatio")
    p["acwr_status_raw"] = acwr.get("acwrStatus")
    p["acwr_status"] = ACWR_STATUS.get(acwr.get("acwrStatus"), acwr.get("acwrStatus"))
    p["acute_load"] = acwr.get("dailyTrainingLoadAcute")
    p["chronic_load"] = acwr.get("dailyTrainingLoadChronic")
    p["vo2max"] = ((ts.get("mostRecentVO2Max") or {}).get("generic") or {}).get("vo2MaxPreciseValue")
    lb = ts.get("mostRecentTrainingLoadBalance", {}).get("metricsTrainingLoadBalanceDTOMap", {})
    lbd = list(lb.values())[0] if lb else {}
    p["load_balance"] = lbd.get("trainingBalanceFeedbackPhrase")
    p["load_alow"] = lbd.get("monthlyLoadAerobicLow")
    p["load_alow_min"] = lbd.get("monthlyLoadAerobicLowTargetMin")
    p["load_alow_max"] = lbd.get("monthlyLoadAerobicLowTargetMax")
    p["load_ahigh"] = lbd.get("monthlyLoadAerobicHigh")
    p["load_ahigh_min"] = lbd.get("monthlyLoadAerobicHighTargetMin")
    p["load_ahigh_max"] = lbd.get("monthlyLoadAerobicHighTargetMax")
    p["load_ana"] = lbd.get("monthlyLoadAnaerobic")
    p["load_ana_min"] = lbd.get("monthlyLoadAnaerobicTargetMin")
    p["load_ana_max"] = lbd.get("monthlyLoadAnaerobicTargetMax")
    devs = ts.get("mostRecentTrainingStatus", {}).get("recordedDevices", [])
    p["device"] = devs[0].get("deviceName") if devs else None

    # --- 睡眠 ---
    sl = (data["sleep"] or {}).get("dailySleepDTO", {})
    sub = sl.get("sleepScores") or {}
    p["sleep_score"] = sub.get("overall", {}).get("value")
    p["sleep_qualifier_raw"] = sub.get("overall", {}).get("qualifierKey")
    p["sleep_qualifier"] = QUAL.get(p["sleep_qualifier_raw"], "未知")
    p["sleep_duration"] = fmt_sec(sl.get("sleepTimeSeconds"))
    p["nap"] = fmt_sec(sl.get("napTimeSeconds"))
    p["deep"] = fmt_sec(sl.get("deepSleepSeconds"))
    p["light"] = fmt_sec(sl.get("lightSleepSeconds"))
    p["rem"] = fmt_sec(sl.get("remSleepSeconds"))
    p["awake"] = fmt_sec(sl.get("awakeSleepSeconds"))
    p["sleep_subs"] = {
        "时长": QUAL.get((sub.get("totalDuration") or {}).get("qualifierKey"), "未知"),
        "压力": QUAL.get((sub.get("stress") or {}).get("qualifierKey"), "未知"),
        "清醒次数": QUAL.get((sub.get("awakeCount") or {}).get("qualifierKey"), "未知"),
        "REM占比": f"{sub.get('remPercentage', {}).get('value', '-')}% " + QUAL.get((sub.get("remPercentage") or {}).get("qualifierKey"), "未知"),
        "浅睡占比": f"{sub.get('lightPercentage', {}).get('value', '-')}% " + QUAL.get((sub.get("lightPercentage") or {}).get("qualifierKey"), "未知"),
        "深睡占比": f"{sub.get('deepPercentage', {}).get('value', '-')}% " + QUAL.get((sub.get("deepPercentage") or {}).get("qualifierKey"), "未知"),
        "不安": QUAL.get((sub.get("restlessness") or {}).get("qualifierKey"), "未知"),
    }
    p["spo2_avg"] = sl.get("averageSpO2Value")
    p["spo2_low"] = sl.get("lowestSpO2Value")
    p["respiration"] = sl.get("averageRespirationValue")
    p["sleep_hr"] = sl.get("avgHeartRate")
    p["sleep_stress"] = sl.get("avgSleepStress")
    need = sl.get("sleepNeed") or {}
    p["sleep_need_actual"] = need.get("actual")
    p["sleep_need_baseline"] = need.get("baseline")

    # --- HRV ---
    hs = (data["hrv"] or {}).get("hrvSummary", {})
    p["hrv_status_raw"] = hs.get("status")
    p["hrv_status"] = HRV_STATUS.get(hs.get("status"), hs.get("status"))
    p["hrv_night"] = hs.get("lastNightAvg")
    p["hrv_5min_high"] = hs.get("lastNight5MinHigh")
    p["hrv_weekly"] = hs.get("weeklyAvg")
    bl = hs.get("baseline") or {}
    p["hrv_baseline_low"] = bl.get("balancedLow")
    p["hrv_baseline_high"] = bl.get("balancedUpper")

    # --- 身体 ---
    p["rhr"] = (data["summary"] or {}).get("restingHeartRate")
    p["rhr_7d"] = (data["summary"] or {}).get("lastSevenDaysAvgRestingHeartRate")
    p["stress_avg"] = (data["summary"] or {}).get("averageStressLevel")
    p["stress_max"] = (data["summary"] or {}).get("maxStressLevel")
    p["steps"] = (data["summary"] or {}).get("totalSteps")
    dist = (data["summary"] or {}).get("totalDistanceMeters")
    p["distance_km"] = round(dist / 1000, 1) if dist else None
    p["calories"] = (data["summary"] or {}).get("totalKilocalories")
    p["has_workout"] = bool(data["next_workout"]) if data["next_workout"] is not None else None

    bb = data["body_battery"]
    p["body_battery"] = None
    p["bb_level"] = None
    if isinstance(bb, list) and bb:
        vals = bb[0].get("bodyBatteryValuesArray", [])
        if vals:
            p["body_battery"] = max(v[1] for v in vals if v[1] is not None)
            p["bb_level"] = "高" if p["body_battery"] >= 75 else ("中等" if p["body_battery"] >= 50 else "低")

    # --- 最近活动（跑步 + 力量，24 小时窗口）---
    p["run_activity"] = None
    p["strength_activity"] = None
    p["run_intensity"] = None
    p["cardiac_drift"] = None

    acts = data.get("activities") or []
    run_act = None
    str_act = None
    for a in acts:
        tk = (a.get("activityType") or {}).get("typeKey")
        if tk == "running" and run_act is None:
            run_act = a
        elif tk == "strength_training" and str_act is None:
            str_act = a

    if run_act:
        ha = hours_ago(run_act)
        if ha is not None and ha <= 24:
            zones = None
            if api is not None:
                zones = safe(lambda: api.get_activity_hr_in_timezones(run_act.get("activityId")))
                p["run_intensity"] = analyze_intensity(api, run_act.get("activityId"))
                p["cardiac_drift"] = analyze_cardiac_drift(api, run_act.get("activityId"))
            r = parse_run(run_act, zones)
            r["device"] = p["device"]
            p["run_activity"] = r

    if str_act:
        ha = hours_ago(str_act)
        if ha is not None and ha <= 24:
            sets = None
            if api is not None:
                sets = safe(lambda: api.get_activity_exercise_sets(str_act.get("activityId")))
                sets = (sets or {}).get("exerciseSets")
            p["strength_activity"] = parse_strength(str_act, sets)

    # --- Garmin Coach 未来 48 小时训练计划 ---
    p["plan_name"] = None
    p["plan_workouts"] = None
    if api is not None:
        plans = safe(lambda: api.get_training_plans())
        pid = None
        if plans and plans.get("trainingPlanList"):
            pid = plans["trainingPlanList"][0].get("trainingPlanId")
            p["plan_name"] = plans["trainingPlanList"][0].get("name")
        if pid:
            detail = safe(lambda: api.get_adaptive_training_plan_by_id(pid))
            tasks = (detail or {}).get("taskList") or []
            d1 = today.isoformat()
            d2 = (today + datetime.timedelta(days=2)).isoformat()
            wl = []
            for t in tasks:
                cd = t.get("calendarDate")
                if cd and d1 <= cd <= d2:
                    tw = t.get("taskWorkout") or {}
                    wl.append({
                        "date": cd,
                        "is_today": cd == d1,
                        "name": tw.get("workoutName"),
                        "desc": tw.get("workoutDescription"),
                        "dur_sec": tw.get("estimatedDurationInSecs"),
                        "effect": tw.get("trainingEffectLabel"),
                        "rest": tw.get("restDay"),
                    })
            p["plan_workouts"] = wl

    return p


# ==================== 判规则区 ====================
def check_rules(p):
    issues = []

    def add(name, fact, sev, advice):
        issues.append((name, fact, sev, advice))

    lb = p["load_balance"]
    if lb in ("HIGH", "OVERREACHING"):
        add("负荷过高", f"负荷平衡={lb}", "红", "未来1~2天强度课降轻松跑或休息")
    elif lb == "ANAEROBIC_SHORTAGE":
        add("无氧不足", "负荷平衡=无氧不足", "黄", "有氧够量了，建议每周补1~2次无氧/间歇")
    elif lb == "LOW_AEROBIC_OVERAGE":
        add("有氧低强度超标", "负荷平衡=有氧低强度过量", "黄", "轻松跑/基础跑量偏多，可适当减少低强度、增加强度训练")
    elif lb == "HIGH_AEROBIC_OVERAGE":
        add("有氧高强度超标", "负荷平衡=有氧高强度过量", "黄", "高强度课偏多，注意穿插轻松日恢复")

    if p["training_status"] == "过度训练":
        add("训练状态告警", "训练状态=过度训练", "红", "今天休息或极轻松活动")
    elif p["training_status"] in ("无成效", "退化停训"):
        add("训练状态告警", f"训练状态={p['training_status']}", "黄", "今天别加量，观察调整")

    if p["sleep_score"] is not None and p["sleep_score"] < 60:
        add("睡眠差", f"睡眠得分={p['sleep_score']}", "黄", "别上强度，今晚早睡")

    if p["hrv_status"] in ("失衡", "偏低", "很差"):
        add("HRV失衡", f"HRV={p['hrv_status']}", "黄", "强度降档，别做间歇")

    if p["rhr"] is not None and p["rhr_7d"] is not None and (p["rhr"] - p["rhr_7d"]) >= 7:
        add("静息心率高", f"今日{p['rhr']} vs 近7天{p['rhr_7d']}", "黄", "休息或极轻松活动")

    if p["body_battery"] is not None and p["body_battery"] < 30:
        add("身体电量低", f"身体电量={p['body_battery']}", "黄", "先充电，强度课换轻松跑或后挪")

    return issues


# ==================== 日报格式区 ====================
def build_report(p, issues, date):
    L = []
    L.append("=" * 48)
    L.append(f"Garmin 训练日报 · {date}")
    L.append("=" * 48)

    # ---------- 第一大类：跑步训练数据 ----------
    L.append("")
    L.append("【第一大类：跑步训练数据】")
    ra = p["run_activity"]
    if ra:
        L.append("")
        L.append("1. 基础环境与时间指标")
        L.append(f"  · 日期与时间：{ra['time']}（{ra['period']}）")
        L.append(f"  · 天气：温度 {ra['temp_min']}~{ra['temp_max']}℃　湿度（）　风速/降雨（）")
        L.append(f"  · 装备：手表 {ra['device'] or '-'}　跑鞋型号（）")
        L.append("")
        L.append("2. 核心配速与距离指标")
        L.append(f"  · 总距离 {ra['distance_km']}km　总时长 {ra['duration']}（含暂停 {ra['elapsed']}）")
        L.append(f"  · 平均配速 {ra['pace']}/km　最快瞬时配速 {ra['best_pace']}/km")
        if ra["splits"]:
            L.append("  · 分段配速：")
            for s in ra["splits"]:
                L.append(f"      - {s['type']}：{s['distance_km']}km　{s['duration']}　{s['pace']}/km")
        else:
            L.append("  · 分段配速：（）")
        L.append("")
        L.append("3. 心肺与心率指标")
        L.append(f"  · 平均心率 {ra['avg_hr']} / 最高心率 {ra['max_hr']}")
        if ra["zones"]:
            zt = "　".join(f"Z{z['zone']} {z['sec']}秒({z['pct']}%)" for z in ra["zones"])
            L.append(f"  · 心率区间：{zt}")
        else:
            L.append("  · 心率区间：（）")
        dr = p["cardiac_drift"]
        if isinstance(dr, dict):
            if dr["verdict"] in ("有", "疑似有"):
                L.append(f"  · 心率漂移：{dr['verdict']}（漂移 +{dr['drift']} bpm）")
                L.append(f"    - 前段 {dr['front_time']}：平均心率 {dr['front_hr']} bpm，配速 {dr['front_pace']}/km")
                L.append(f"    - 后段 {dr['back_time']}：平均心率 {dr['back_hr']} bpm，配速 {dr['back_pace']}/km")
            else:
                L.append(f"  · 心率漂移：{dr['verdict']}")
        else:
            L.append(f"  · 心率漂移：{dr if dr else '（）'}")
        L.append("")
        L.append("4. 跑姿与生物力学指标")
        L.append(f"  · 平均步频 {ra['avg_cadence']} / 最高步频 {ra['max_cadence']} 步/分")
        L.append(f"  · 平均步幅 {ra['stride']}m")
        L.append(f"  · 垂直振幅 {ra['vo_osc']}cm　垂直步幅比 {ra['vert_ratio']}%")
        L.append(f"  · 触地时间 {ra['gct']}ms")
        L.append("")
        L.append("5. 负荷与恢复指标")
        L.append(f"  · 训练效果：有氧 {ra['aerobic_te']} / 无氧 {ra['anaerobic_te']}")
        L.append(f"  · 运动负荷 {ra['training_load']}")
        L.append(f"  · 卡路里 {ra['calories']}　预估汗液流失 {ra['sweat'] if ra['sweat'] else '（）'}ml")
        L.append("")
        L.append("6. 高强度段分析（冲刺/间歇/阈值）")
        ri = p["run_intensity"]
        if ri:
            L.append(f"  · 识别出 {ri['segments']} 个高强度段（配速快于 {ri['threshold_pace']}/km），累计 {ri['duration']}")
            L.append(f"  · 步频：平均 {ri['cad_avg']} / 最高 {ri['cad_max']} 步/分")
            L.append(f"  · 心率：平均 {ri['hr_avg']} / 最高 {ri['hr_max']} bpm")
            L.append(f"  · 触地时间：平均 {ri['gct_avg']}ms / 最短 {ri['gct_min']}ms　腾空时间 {ri['air_avg']}ms")
            L.append(f"  · 触地腾空比：{ri['gct_ratio']}")
            L.append(f"  · 垂直振幅 {ri['osc_avg']}cm　步幅 {ri['stride_avg']}cm")
            if ri['hr_recovery']:
                L.append(f"  · 心率恢复速度：峰值后 2 分钟下降 {ri['hr_recovery']} bpm")
        else:
            L.append("  · （本次无高强度段，为匀速跑）")
        L.append("")
        L.append("7. 主观与伤病预警（需手动填写）")
        L.append("  · 旧伤反馈：左脚踝内侧胫骨后肌腱是否不适（）时间点（）评分0-10（）继续/降速/停止（）")
        L.append("  · 其他不适：胫骨前肌（）右跟腱（）腰椎（）膝盖（）")
        L.append("  · 体感：轻松/有点累/很累（）　抽筋前兆（）")
        L.append("  · 跑后处理：冰敷15分钟（）　脚踝等长收缩（）")
    else:
        L.append("（前 24 小时无跑步记录）")

    # ---------- 第二大类：力量训练数据 ----------
    L.append("")
    L.append("【第二大类：力量训练数据】")
    sa = p["strength_activity"]
    if sa:
        L.append("")
        L.append("1. 基础训练指标")
        L.append(f"  · 日期：{sa['time']}　总时长 {sa['duration']}　总组数 {sa['set_count']}　总消耗 {sa['calories']}千卡")
        L.append("")
        L.append("2. 核心动作详细指标（需手动填写）")
        L.append("  · 深蹲：重量kg/次数/组数/组间休息（）")
        L.append("  · 硬拉：（）")
        L.append("  · 卧推：（）")
        L.append("  · 引体向上：每组次数/是否负重/组数（）")
        L.append("  · 平板支撑：每组时长秒/组数（）")
        L.append("  · 弹力带90度肩外旋：阻力kg/次数/组数（）")
        L.append("  · 其他动作：（）")
        L.append("")
        L.append("3. 容量与负荷指标（需手动）")
        L.append("  · 总容量（kg×次数×组数）：（）")
        L.append("  · 运动负荷：（）")
        L.append("")
        L.append("4. 主观与伤病预警（需手动填写）")
        L.append("  · 动作降级情况：是否降重量/降到多少kg（）")
        L.append("  · 腰部反馈：深蹲/硬拉腰部压力或牵扯感（）")
        L.append("  · 脚踝反馈：负重时左脚踝内侧隐痛（）")
        L.append("  · 执行感受：轻松/正常/力有不逮（）")
    else:
        L.append("（前 24 小时无力量训练记录）")

    # ---------- 第三大类：晨起恢复数据 ----------
    L.append("")
    L.append("【第三大类：晨起恢复数据】")
    L.append("")
    L.append("1. 睡眠数据")
    L.append(f"  · 睡眠评分：{p['sleep_score'] if p['sleep_score'] is not None else '-'}（{p['sleep_qualifier']}）")
    L.append(f"  · 总时长 {p['sleep_duration']}　午睡 {p['nap']}")
    L.append(f"  · 深睡 {p['deep']} / 浅睡 {p['light']} / REM {p['rem']} / 清醒 {p['awake']}")
    L.append(f"  · 分项：时长[{p['sleep_subs']['时长']}] 压力[{p['sleep_subs']['压力']}] 清醒次数[{p['sleep_subs']['清醒次数']}]")
    L.append(f"  · 分项：REM[{p['sleep_subs']['REM占比']}] 浅睡[{p['sleep_subs']['浅睡占比']}] 深睡[{p['sleep_subs']['深睡占比']}] 不安[{p['sleep_subs']['不安']}]")
    L.append(f"  · 血氧 {p['spo2_avg']}%（最低 {p['spo2_low']}%）　呼吸 {p['respiration']}次/分")
    L.append(f"  · 睡眠心率 {p['sleep_hr']}　睡眠压力 {p['sleep_stress']}")
    L.append(f"  · 睡眠需求：实际 {p['sleep_need_actual']} / 基准 {p['sleep_need_baseline']} 分钟")
    L.append("")
    L.append("2. HRV 状态")
    L.append(f"  · 状态 {p['hrv_status']}　昨晚均值 {p['hrv_night']}ms　昨晚5分钟最高 {p['hrv_5min_high']}ms")
    L.append(f"  · 7天均值 {p['hrv_weekly']}ms　平衡区间 {p['hrv_baseline_low']}~{p['hrv_baseline_high']}ms")
    L.append("")
    L.append("3. 静息心率")
    L.append(f"  · 今晨 {p['rhr']}　对比7天均值 {p['rhr_7d']}")
    L.append("")
    L.append("4. 身体电量")
    L.append(f"  · 晨起 {p['body_battery']}（{p['bb_level'] or '-'}）")
    L.append("")
    L.append("5. 其他日常指标")
    L.append(f"  · 压力：平均 {p['stress_avg']} / 最高 {p['stress_max']}")
    L.append(f"  · 步数 {p['steps']}　距离 {p['distance_km']}km　消耗 {p['calories']}千卡")
    L.append(f"  · 今天课表：{'有安排' if p['has_workout'] else '无计划'}")
    L.append("")
    L.append("6. 旧伤晨起专项（需手动填写）")
    L.append("  · 左脚踝内侧胫骨后肌腱（）　腰椎（）　右脚跟腱（）")

    # ---------- 总结 ----------
    red = [i for i in issues if i[2] == "红"]
    yellow = [i for i in issues if i[2] == "黄"]
    if red:
        summary = f"今天身体明显没恢复（{len(red)} 项红），不适合跑强度课。"
    elif yellow:
        summary = f"今天状态总体还行（{len(yellow)} 项提醒），照常训练，注意下面几点。"
    else:
        summary = "今天各项指标正常，可以照课表练。"

    L.append("")
    L.append("【总结】")
    L.append(summary)
    if issues:
        L.append("")
        L.append("问题与建议：")
        for name, fact, sev, advice in issues:
            icon = "🔴" if sev == "红" else "🟡"
            L.append(f"{icon} [{name}] {sev}｜{fact}")
            L.append(f"   → {advice}")
    else:
        L.append("（没有命中任何问题。）")

    # ---------- 训练计划 ----------
    L.append("")
    L.append(f"【训练计划（Garmin Coach · {p.get('plan_name') or '训练计划'}）】")
    wl = p.get("plan_workouts")
    if wl is None:
        L.append("  （未获取到教练计划）")
    elif not wl:
        L.append("  （今天及未来 48 小时没有训练安排）")
    else:
        for w in wl:
            tag = "今天" if w.get("is_today") else w["date"]
            if w["rest"]:
                L.append(f"  · {tag}：休息日")
            else:
                parts = []
                if w["name"]:
                    parts.append(w["name"])
                if w["desc"]:
                    parts.append(f"配速 {w['desc']}")
                if w["dur_sec"]:
                    parts.append(f"约 {int(w['dur_sec']) // 60} 分钟")
                if w["effect"]:
                    parts.append(TE_MAP.get(w["effect"], w["effect"]))
                L.append("  · %s：%s" % (tag, "｜".join(parts)))

    # ---------- 原始数据 ----------
    L.append("")
    L.append("【原始数据（可复制导出）】")
    L.append(json.dumps(p, ensure_ascii=False, default=str))
    L.append("")
    return "\n".join(L)


# ==================== 主流程 ====================
def main():
    log.info("=== Garmin 日报任务启动 ===")

    now_cn = datetime.datetime.now(CN_TZ)
    report_date = (now_cn.date() - datetime.timedelta(days=DATE_OFFSET)).isoformat()
    log.info("报告日期=%s（执行日=%s，offset=%d）",
             report_date, now_cn.date().isoformat(), DATE_OFFSET)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / f"日报_{report_date}.txt"

    try:
        api = login_garmin()
        data = fetch_all(api, report_date)
        p = parse(data, api, report_date)
        issues = check_rules(p)
        report = build_report(p, issues, report_date)

        out_path.write_text(report, encoding="utf-8")
        log.info("日报已写入 %s（%d 字节）", out_path, out_path.stat().st_size)

        push_wecom(build_push_summary(p, issues, report_date, out_path))

        cleanup_old_reports(REPORT_DIR, RETENTION_DAYS)
        log.info("=== 任务完成 ===")
        return 0
    except Exception as e:
        log.exception("任务失败：%s", e)
        push_wecom(f"⚠️ **Garmin 日报任务失败**\n> {report_date}\n> {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
