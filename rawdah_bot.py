# -*- coding: utf-8 -*-
"""
Rawdah booking userbot v3.1 (Telethon)

- بيحجز أي كمية يلاقيها (حجز جزئي)، وبعد كل دفعة بيخصم من الطلب ويبلّغ العميل.
- لو لقى زرار "قبول الكل" بيضغطه، ولو لقى زرار نافذة فيها مقاعد بيدخل عليها.
- بيضغط "تحديث" كل REFRESH_EVERY ثانية، وبيشتغل على أحداث تليجرام (مش polling) عشان السرعة.
- بيختار الجنسية المطلوبة (أو "أي جنسية") ويكمّل لحد زرار التأكيد ويضغطه فورًا.
- لما الكمية تخلص بيوقف نفسه، وأي طلب جديد بيشغّله من تاني.

v3.1: إصلاح تخطّي شاشة المواعيد (نافذة 03:00 – 02:40 · 20 مقعدًا · حتى 02:08)
"""
import asyncio
import logging
import re
import time
from collections import deque

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError

# ===================== الإعدادات =====================
API_ID = 37543339
API_HASH = "c4c41e2ab95d61a576f7798415203bd2"   # سرّي: ماتبعتوش لحد
SESSION = "rawdah1_bot"

TARGET_USERNAME = "RawdahBrokerBot"   # لو غلط الكود بيدور بالاسم تلقائيًا
TARGET_NAME_HINT = "RawdahBroker"

ALLOWED_USER_IDS = []
ALLOW_EVERYONE = True

REFRESH_EVERY = 2.0             # كل كام ثانية يضغط "تحديث" (قللها بحذر: الضغط الزائد ممكن يعمل FloodWait)
STEP_TIMEOUT = 3.0              # أقصى انتظار لرد البوت المستهدف بعد كل ضغطة
MAX_CONFIRM_FAILS = 3           # عدد محاولات فشل التأكيد قبل ما يوقف
# =====================================================

BOT_MARK = "🤖 "
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("rawdah")

client = None
TARGET = None
TARGET_ID = None
ME = None
TARGET_LOCK = asyncio.Lock()
JOBS, CONVOS, PEERS = {}, {}, {}

# ---------- ذاكرة أحداث البوت المستهدف ----------
UPDATES = deque(maxlen=300)     # (seq, kind, msg)
LATEST = {}                     # msg_id -> آخر نسخة
SEQ = 0
UPDATE_EVT = None               # بيتعمل في main
ACCEPTED = {}                   # توقيع رسالة -> وقت آخر ضغط "قبول الكل"
BG = set()

# رموز الاتجاه/الفراغ الخفية اللي تليجرام بيحطها في النصوص المختلطة عربي/إنجليزي
INVISIBLE = "\u200b-\u200f\u202a-\u202e\u2060\u2066-\u2069\ufeff"


def norm(s: str) -> str:
    s = re.sub(rf"[\u064B-\u0652\u0640{INVISIBLE}]", "", s or "")
    s = s.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    s = re.sub("[إأآ]", "ا", s)
    s = s.replace("ة", "ه").replace("ى", "ي")
    return re.sub(r"\s+", " ", s).strip()


def num(pattern, text):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


def to_int(s):
    m = re.search(r"\d+", s or "")
    return int(m.group()) if m else 0


def btns(msg):
    return [b for row in (msg.buttons or []) for b in row]


def find_btn(msg, pred):
    for b in btns(msg):
        if pred(norm(b.text)):
            return b
    return None


def signature(msg):
    return (msg.raw_text, tuple(b.text for b in btns(msg)))


def parse_balance(text):
    return num(r"الرصيد\D{0,6}(\d+)", norm(text))


SEAT_WORD = r"مق(?:ا)?عد"


def seat_count(t):
    """عدد المقاعد من نص زرار/رسالة: '20 مقعدًا' أو '4 مقاعد' أو 'مقاعد 4' أو 'عدد المقاعد: 4'."""
    t = norm(t)
    m = re.search(rf"(\d+)\s*{SEAT_WORD}", t) or re.search(rf"{SEAT_WORD}\w*\s*:?\s*(\d+)", t)
    return int(m.group(1)) if m else None


def screen(msg) -> str:
    t = norm(msg.raw_text)
    if "تاكيد الشراء" in t and find_btn(msg, lambda x: "تاكيد" in x or "قبول الكل" in x):
        return "confirm"
    if "كم مقعد" in t:
        return "count"
    if "جنسيه تريدون" in t:
        return "country"
    # شاشة المواعيد: "المتاح الآن · رجال ... اختر نافذة ..."
    if "المتاح الان" in t[:90] or ("اختر نافذه" in t and slot_buttons(msg)):
        return "slots"
    return "other"


FLOW = ("country", "count", "confirm")


def push(kind, msg):
    global SEQ
    SEQ += 1
    UPDATES.append((SEQ, kind, msg))
    LATEST[msg.id] = msg
    log.debug("event %s id=%s screen=%s", kind, msg.id, screen(msg))
    UPDATE_EVT.set()


async def wait_update(since, timeout, accept):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        UPDATE_EVT.clear()
        for seq, kind, m in list(UPDATES):
            if seq > since and accept(kind, m):
                return m
        left = end - loop.time()
        if left <= 0:
            return None
        try:
            await asyncio.wait_for(UPDATE_EVT.wait(), left)
        except asyncio.TimeoutError:
            return None


async def safe_click(btn):
    try:
        await btn.click()
        log.info("ضغطت على '%s' ✔", norm(btn.text)[:30])
        return True
    except FloodWaitError as e:
        log.warning("FloodWait %ss", e.seconds)
        await asyncio.sleep(e.seconds + 1)
    except Exception as e:
        log.info("ضغطة '%s' رجّعت: %r (ممكن الضغطة اتنفذت برضه)", norm(btn.text)[:30], e)
    return False


def fire(btn):
    t = asyncio.create_task(safe_click(btn))
    BG.add(t)
    t.add_done_callback(BG.discard)


async def fetch(msg_id):
    try:
        return await client.get_messages(TARGET, ids=msg_id)
    except Exception as e:
        log.info("fetch error: %r", e)
        return None


async def click_step(msg, btn, timeout=STEP_TIMEOUT, want=FLOW):
    """اضغط واستنى الشاشة المطلوبة فقط (تعديل نفس الرسالة أو رسالة جديدة).
    أي تعديل تاني (زي تحديث الوقت في شاشة المواعيد) بيتجاهل."""
    since = SEQ
    fire(btn)
    res = await wait_update(
        since, timeout,
        lambda k, m: screen(m) in want and (m.id == msg.id or k == "new"))
    if res is None:
        # احتياطي: اقرا الرسالة مباشرة (ممكن الحدث اتأخر أو ضاع)
        cur = await fetch(msg.id)
        if cur is not None:
            LATEST[cur.id] = cur
            if screen(cur) in want:
                return cur
            log.info("click_step: الشاشة الحالية '%s' مش المطلوبة %s", screen(cur), want)
    return res


def note(job, text):
    job.status = text
    if text != job.last_note:
        job.last_note = text
        log.info("[%s] %s", job.chat_id, text)


async def say(chat_id, text):
    await client.send_message(PEERS.get(chat_id, chat_id), BOT_MARK + text)


class Job:
    def __init__(self, chat_id, gender, country, count):
        self.chat_id = chat_id
        self.gender = gender
        self.country = country
        self.initial_count = count
        self.remaining_count = count
        self.stop = False
        self.wake = asyncio.Event()
        self.task = None
        self.status = "بدأت المراقبة"
        self.last_note = ""
        self.fails = 0


# ---------------------- القوائم ----------------------
def _is_list(m, job):
    t = norm(m.raw_text)
    return screen(m) == "slots" and norm(job.gender) in t[:90]


def cached_list(job):
    for mid in sorted(LATEST, reverse=True)[:40]:
        m = LATEST[mid]
        if not m.out and _is_list(m, job):
            return m
    return None


async def recent_target_msgs(limit=15):
    try:
        msgs = [m for m in await client.get_messages(TARGET, limit=limit) if not m.out]
        for m in msgs:
            LATEST.setdefault(m.id, m)
        return msgs
    except Exception as e:
        log.info("recent msgs error: %r", e)
        return []


def _gender_btn(msg, job):
    return find_btn(msg, lambda t: "المتاح الان" in t and norm(job.gender) in t)


async def get_list_msg(job):
    """بحث عبر API (للبداية بس، بعدها بنشتغل على الأحداث)."""
    msgs = await recent_target_msgs()
    m = next((x for x in msgs if _is_list(x, job)), None)
    if m:
        return m
    menu = next((x for x in msgs if _gender_btn(x, job)), None)
    if not menu:
        await client.send_message(TARGET, "/start")
        end = asyncio.get_running_loop().time() + 4
        while asyncio.get_running_loop().time() < end and not menu:
            await asyncio.sleep(0.3)
            menu = next((x for x in await recent_target_msgs(5) if _gender_btn(x, job)), None)
    if not menu:
        return None
    await safe_click(_gender_btn(menu, job))
    end = asyncio.get_running_loop().time() + 4
    while asyncio.get_running_loop().time() < end:
        await asyncio.sleep(0.2)
        m = next((x for x in await recent_target_msgs(8) if _is_list(x, job)), None)
        if m:
            return m
    return None


def slot_buttons(msg):
    """نصوص أزرار النوافذ اللي فيها مقاعد > 0، مثال: '03:00 – 02:40 · 20 مقعدًا · حتى 02:08'."""
    out = []
    for b in btns(msg):
        t = norm(b.text)
        if "تحديث" in t or "رجوع" in t or not re.search(SEAT_WORD, t):
            continue
        n = seat_count(t)
        if n is None or n > 0:
            out.append(b.text)
    return out


COUNTRY_RE = re.compile(r"^(.*?)\s*[·:：•|\-–]\s*(\d+)$")
ANY_WORDS = ("اي", "اي جنسيه", "جنسيات", "جنسيه", "الكل", "any")


def pick_country(msg, want_raw):
    want = norm(want_raw)
    any_c = (not want) or want in ANY_WORDS or "جنسي" in want
    best = None
    for x in btns(msg):
        t = norm(x.text)
        if "رجوع" in t or "تغيير" in t:
            continue
        m = COUNTRY_RE.match(t)
        if not m or int(m.group(2)) <= 0:
            continue
        cnt = int(m.group(2))
        if any_c:
            if t.startswith("اي جنسيه"):
                return x, cnt
            if best is None or cnt > best[1]:
                best = (x, cnt)
        elif want in m.group(1):
            return x, cnt
    return best if any_c else None


# ---------------------- الحجز ----------------------
async def after_slot(job, msg, slot_text, balance):
    scr, avail = screen(msg), None

    if scr == "country":
        pick = pick_country(msg, job.country)
        if not pick:
            note(job, f"الدولة '{job.country}' مش متاحة في نافذة {slot_text[:20]} - برجع وأجرب غيرها")
            back = find_btn(msg, lambda t: "رجوع" in t)
            if back:
                await click_step(msg, back, want=("slots",))
            return None
        cbtn, avail = pick
        note(job, f"اخترت {norm(cbtn.text)}")
        msg = await click_step(msg, cbtn, want=("count", "confirm"))
        if not msg:
            return None
        scr = screen(msg)

    if scr == "confirm":                       # بعض الإصدارات بتروح للتأكيد مباشرة
        return {"msg": msg, "seats": avail, "slot": slot_text}

    if scr != "count":
        note(job, f"شاشة غير متوقعة ({scr}) بعد اختيار الميعاد/الجنسية: {norm(msg.raw_text)[:60]}")
        return None

    t = norm(msg.raw_text)
    b2 = parse_balance(t)
    if b2 is not None:
        balance = b2
    if balance == 0:
        return {"no_balance": True}
    if avail is None:
        avail = num(r"(\d+)\s*مقعد\w*\s*متاح", t) or job.remaining_count

    n = min(job.remaining_count, avail, balance if balance is not None else job.remaining_count)
    if n <= 0:
        return None

    since = SEQ
    cb = find_btn(msg, lambda x: x == str(n)) or \
        find_btn(msg, lambda x: x.startswith("الكل") and to_int(x) == n)
    if cb:
        fire(cb)
    else:
        other = find_btn(msg, lambda x: "اكتب" in x)
        if not other:
            note(job, "شاشة العدد: ما لقيتش زرار الرقم ولا 'اكتب عددًا آخر'")
            return None
        await safe_click(other)
        await client.send_message(TARGET, str(n))

    conf = await wait_update(since, STEP_TIMEOUT, lambda k, m: screen(m) == "confirm")
    if not conf:
        note(job, "ما وصلتش لشاشة 'تأكيد الشراء'")
        return None
    return {"msg": conf, "seats": n, "slot": slot_text}


def find_accept_all(job):
    other = "نساء" if norm(job.gender) == norm("رجال") else "رجال"
    now = time.monotonic()
    for mid in sorted(LATEST, reverse=True)[:12]:
        m = LATEST[mid]
        if m.out:
            continue
        t = norm(m.raw_text)
        if norm(other) in t[:60] and norm(job.gender) not in t[:60]:
            continue
        b = find_btn(m, lambda x: "قبول الكل" in x)
        if b and now - ACCEPTED.get(signature(m), 0) > 10:
            return m, b
    return None


async def take_accept_all(job, msg, btn):
    note(job, "لقيت زرار 'قبول الكل' - بضغط عليه")
    ACCEPTED[signature(msg)] = time.monotonic()
    since = SEQ
    fire(btn)
    final = await wait_update(since, STEP_TIMEOUT, lambda k, m: m.id == msg.id or k == "new")
    if final and screen(final) == "confirm":
        return {"msg": final, "seats": None, "slot": "قبول الكل"}
    text = norm(final.raw_text) if final else ""
    taken = seat_count(text)
    if taken:
        return {"done": taken, "slot": "قبول الكل", "reply": final.raw_text}
    return {"unknown": True, "reply": final.raw_text if final else ""}


async def try_book(job):
    hit = find_accept_all(job)
    if hit:
        return await take_accept_all(job, *hit)

    # لو فيه عملية واقفة في نص الطريق كمّل منها
    top = LATEST[max(LATEST)] if LATEST else None
    if top and not top.out and screen(top) in ("country", "count") and norm(job.gender) in norm(top.raw_text):
        res = await after_slot(job, top, norm(top.raw_text).split("\n")[0], None)
        if res:
            return res

    lst = cached_list(job) or await get_list_msg(job)
    if not lst:
        note(job, f"مش لاقي رسالة 'المتاح الآن · {job.gender}' في محادثة البوت")
        return None

    balance = parse_balance(lst.raw_text)
    slots = slot_buttons(lst)
    log.info("رسالة المواعيد id=%s | أزرار: %s | نوافذ فيها مقاعد: %d",
             lst.id, " ; ".join(norm(b.text)[:25] for b in btns(lst)), len(slots))
    if not slots:
        note(job, f"{job.gender}: مفيش مقاعد متاحة حاليًا (الرصيد {balance})")
        return None
    if balance == 0:
        return {"no_balance": True}

    note(job, f"لقيت {len(slots)} نافذة فيها مقاعد: " + " | ".join(norm(s)[:28] for s in slots))
    for slot_text in slots:
        lst = cached_list(job) or lst
        b = find_btn(lst, lambda t, s=norm(slot_text): t == s)
        if not b:
            continue
        nxt = await click_step(lst, b)
        if not nxt:
            note(job, f"ضغطت على '{norm(slot_text)[:28]}' بس ما جاتش الشاشة التالية")
            continue
        res = await after_slot(job, nxt, slot_text, balance)
        if res:
            return res
    return None


async def confirm_and_report(job, res):
    """يضغط التأكيد فورًا، يقرا العدد اللي اتاخد فعلًا، يخصمه ويبلّغ العميل. يرجّع True لو نجح."""
    chat, conf = job.chat_id, res["msg"]
    ct = norm(conf.raw_text)
    taken = seat_count(ct) or res.get("seats") or 0
    left = num(r"يبقي\s*(\d+)", ct)

    btn = find_btn(conf, lambda t: "قبول الكل" in t or "تاكيد" in t)
    if not btn:
        note(job, "ما لقيتش زرار التأكيد")
        return False
    since = SEQ
    fire(btn)
    final = await wait_update(since, STEP_TIMEOUT, lambda k, m: m.id == conf.id or k == "new")
    if final is None:
        final = await fetch(conf.id)
    failed = bool(final) and (screen(final) == "confirm" or re.search(
        r"فشل|خطا|لم يتم|غير كاف|انتهت|تعذر", norm(final.raw_text)))
    if failed:
        job.fails += 1
        await say(chat, f"⚠️ ما تأكدش الحجز (محاولة {job.fails}/{MAX_CONFIRM_FAILS}):\n"
                        f"{final.raw_text[:200]}")
        return False

    job.fails = 0
    if taken <= 0:
        await say(chat, f"✅ تم الضغط على التأكيد، راجع رد البوت المستهدف لمعرفة العدد:\n"
                        f"{final.raw_text if final else ''}")
        job.stop = True
        return True

    job.remaining_count = max(0, job.remaining_count - taken)
    await say(chat, f"✅ تم أخذ {taken} مقعد\n📌 وظلّ من طلبكم: {job.remaining_count} مقعد"
                    f"\n🕒 {res.get('slot', '')}")
    if job.remaining_count > 0 and left == 0:
        await say(chat, f"⚠️ رصيدكم انتهى. يرجى إعادة شحن رصيدكم في بوت @{TARGET.username}")
        job.stop = True
    return True


async def monitor(job: Job):
    chat = job.chat_id
    loop = asyncio.get_running_loop()
    log.info("monitor start: %s | %s | %s", job.gender, job.country, job.remaining_count)
    last_refresh = 0.0

    while not job.stop and job.remaining_count > 0:
        job.wake.clear()
        res = None
        try:
            async with TARGET_LOCK:
                res = await try_book(job)
                if res and res.get("msg"):
                    ok = await confirm_and_report(job, res)
                    if not ok and job.fails >= MAX_CONFIRM_FAILS:
                        await say(chat, "⛔ توقفت بعد فشل التأكيد أكتر من مرة. ابعت /start لو عايز تعيد.")
                        break
                    continue
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            continue
        except (ConnectionError, OSError):
            await asyncio.sleep(5)
            continue
        except Exception:
            log.exception("monitor error")
            note(job, "حصل خطأ داخلي (شوف اللوج)")
            await asyncio.sleep(2)

        if res and res.get("no_balance"):
            await say(chat, f"رصيدك خلصان. يرجى إعادة شحن رصيدك في بوت @{TARGET.username}")
            break
        if res and res.get("done"):                      # قبول الكل رجّع عدد صريح
            job.remaining_count = max(0, job.remaining_count - res["done"])
            await say(chat, f"✅ تم أخذ {res['done']} مقعد\n📌 وظلّ من طلبكم: {job.remaining_count} مقعد")
            continue
        if res and res.get("unknown"):
            await say(chat, f"⚠️ ضغطت 'قبول الكل' بس ما قدرتش أعرف العدد. رد البوت:\n{res['reply'][:300]}")
            break

        now = loop.time()
        if now - last_refresh >= REFRESH_EVERY:         # تحديث كل كام ثانية
            lst = cached_list(job)
            rb = find_btn(lst, lambda t: "تحديث" in t or "حدث" in t) if lst else None
            if rb:
                fire(rb)
            last_refresh = now
        wait = max(0.05, REFRESH_EVERY - (loop.time() - last_refresh))
        try:
            await asyncio.wait_for(job.wake.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass

    JOBS.pop(chat, None)
    if job.remaining_count <= 0:
        await say(chat, f"🎉 تم أخذ جميع الكمية المطلوبة ({job.initial_count} مقعد) ✅\n"
                        f"لو عايز طلب جديد ابعت /start")
    log.info("monitor end")


# ---------------------- محادثة العميل ----------------------
CONFIRM_WORDS = {"تاكيد", "اكد", "confirm", "yes", "ايوه", "اه"}


def parse_gender(t):
    t = norm(t)
    if any(w in t for w in ("رجال", "رجل", "men", "male")):
        return "رجال"
    if any(w in t for w in ("نساء", "نسا", "سيدات", "women", "female")):
        return "نساء"
    return None


async def on_message(event):
    if not event.is_private or not event.raw_text:
        return
    text = event.raw_text.strip()
    if text.startswith(BOT_MARK.strip()):
        return
    chat = event.chat_id
    if event.out and chat != ME.id:
        return
    if chat == TARGET_ID or (event.sender and getattr(event.sender, "bot", False)):
        return
    if not (ALLOW_EVERYONE or chat == ME.id or event.sender_id in ALLOWED_USER_IDS):
        return
    try:
        PEERS[chat] = await event.get_input_chat()
    except Exception:
        pass

    n = norm(text).lower()
    job = JOBS.get(chat)

    if n in ("/stop", "stop", "وقف"):
        if job:
            job.stop = True
            job.wake.set()
        JOBS.pop(chat, None)
        CONVOS.pop(chat, None)
        await say(chat, "تم الإيقاف.")
        return

    if n in ("/debug", "debug"):
        msgs = await recent_target_msgs(6)
        lines = [f"البوت المستهدف: @{TARGET.username} (id {TARGET.id})",
                 "المراقبة: " + (f"شغالة - {job.status}" if job else "مش شغالة")]
        for m in msgs[:5]:
            lines.append(f"#{m.id} [{screen(m)}] {norm(m.raw_text)[:60]}\n   أزرار: "
                         + " ; ".join(norm(b.text)[:24] for b in btns(m)[:6]))
        if not msgs:
            lines.append("مفيش ولا رسالة من البوت المستهدف! (غالبًا اليوزر غلط)")
        await say(chat, "\n".join(lines))
        return

    if n in ("/status", "status", "حالة"):
        if not job:
            await say(chat, "مفيش مراقبة شغالة. ابعت /start")
        else:
            await say(chat, f"المراقبة شغالة ✅\nالنوع: {job.gender} | الدولة: {job.country}\n"
                            f"المطلوب: {job.initial_count} | المتبقي: {job.remaining_count}\n"
                            f"آخر حالة: {job.status}")
        return

    if n in ("/start", "start", "ابدا", "حجز"):
        CONVOS[chat] = {"stage": "gender"}
        await say(chat, "الروضة الشريفة - مسار فوري\nرجال ولا نساء؟")
        return

    st = CONVOS.get(chat)
    if not st:
        return
    if st["stage"] == "gender":
        g = parse_gender(text)
        if not g:
            await say(chat, "اكتب: رجال أو نساء")
            return
        st.update(gender=g, stage="country")
        await say(chat, "أي جنسية أم دولة محددة؟ (اكتب اسم الدولة أو كلمة: جنسيات)")
    elif st["stage"] == "country":
        st.update(country=text, stage="count")
        await say(chat, "عدد المقاعد الإجمالي المطلوب؟")
    elif st["stage"] == "count":
        c = to_int(norm(text))
        if c <= 0:
            await say(chat, "اكتب رقم صحيح")
            return
        CONVOS.pop(chat)
        if chat in JOBS:
            JOBS[chat].stop = True
            JOBS[chat].wake.set()
        job = Job(chat, st["gender"], st["country"], c)
        JOBS[chat] = job
        job.task = asyncio.create_task(monitor(job))
        await say(chat, f"✅ تم البدء!\nالنوع: {job.gender} | الدولة: {job.country} | المطلوب: {c} مقعد.\n"
                        f"أي كمية تظهر هحجزها فورًا وأبلغك بالمتبقي. /status لمعرفة الحالة.")


async def on_target_new(event):
    push("new", event.message)
    for j in JOBS.values():
        j.wake.set()


async def on_target_edit(event):
    push("edit", event.message)
    for j in JOBS.values():
        j.wake.set()


# ---------------------- التشغيل ----------------------
async def resolve_target():
    try:
        return await client.get_entity(TARGET_USERNAME)
    except Exception as e:
        log.warning("مش لاقي @%s (%s) - هدور بالاسم في محادثاتك", TARGET_USERNAME, e)
    async for d in client.iter_dialogs():
        name = re.sub(r"\W", "", (d.name or "").lower())
        if getattr(d.entity, "bot", False) and TARGET_NAME_HINT.lower() in name:
            return d.entity
    return None


async def main():
    global client, ME, TARGET, TARGET_ID, UPDATE_EVT
    UPDATE_EVT = asyncio.Event()
    client = TelegramClient(SESSION, API_ID, API_HASH)
    await client.start()
    ME = await client.get_me()
    TARGET = await resolve_target()
    if TARGET is None:
        log.error("ما لقيتش البوت المستهدف. افتحه من حسابك وابعتله /start وشغّل تاني.")
        return
    TARGET_ID = TARGET.id
    log.info("logged in as %s (%s) | target: @%s", ME.first_name, ME.id, TARGET.username)

    client.add_event_handler(on_message, events.NewMessage())
    client.add_event_handler(on_target_new, events.NewMessage(chats=TARGET, incoming=True))
    client.add_event_handler(on_target_edit, events.MessageEdited(chats=TARGET, incoming=True))
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())