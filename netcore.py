# -*- coding: utf-8 -*-
"""
netcore.py - measurement engine for Net Optimizer (no UI code, runs on Android and desktop).

DNS : raw UDP DNS queries  -> latency (avg/min/median), jitter, loss, score
MTU : ping with Don't-Fragment (/system/bin/ping on Android) -> largest working MTU,
      then latency / jitter / loss for the best candidate sizes.
"""

import csv
import ipaddress
import math
import os
import random
import re
import shutil
import socket
import ssl
import statistics
import struct
import subprocess
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

APP_NAME = "Net Optimizer"
APP_VERSION = "1.0"

# The UI sets this to a folder the user can reach; falls back to the current folder.
RESULTS_DIR = None


# ----------------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------------
def fnum(v, nd=1):
    """Number for CSV: empty when infinite/None."""
    if v is None or (isinstance(v, float) and (math.isinf(v) or math.isnan(v))):
        return ""
    return round(v, nd)


def fdisp(v, nd=1):
    """Number for the screen: dash when infinite/None."""
    if v is None or (isinstance(v, float) and (math.isinf(v) or math.isnan(v))):
        return "-"
    return "{:.{}f}".format(v, nd)


def jitter_of(samples):
    """Jitter = mean absolute difference between consecutive samples (ms)."""
    if len(samples) < 2:
        return 0.0
    return statistics.mean(abs(b - a) for a, b in zip(samples, samples[1:]))


def results_dir():
    base = RESULTS_DIR or os.path.join(os.getcwd(), "NetOptimizer_results")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:  # noqa: BLE001
        base = os.getcwd()
    return base


def csv_text(header, rows):
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def write_csv(prefix, header, rows):
    """Write a CSV (UTF-8 with BOM so Excel opens it correctly). Returns (path, text)."""
    text = csv_text(header, rows)
    path = os.path.join(results_dir(), "{}_{}.csv".format(prefix, time.strftime("%Y%m%d_%H%M%S")))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        f.write(text)
    return path, text


def append_history(test, best, avg, jit, loss, extra=""):
    path = os.path.join(results_dir(), "history.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["Timestamp", "Test", "Best", "Latency avg ms", "Jitter ms", "Loss %", "Notes"])
        w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), test, best, fnum(avg), fnum(jit), fnum(loss, 0), extra])


# ----------------------------------------------------------------------------
# DNS
# ----------------------------------------------------------------------------
DNS_SERVERS = [
    ("Cloudflare", "1.1.1.1", "1.0.0.1"),
    ("Google", "8.8.8.8", "8.8.4.4"),
    ("Quad9", "9.9.9.9", "149.112.112.112"),
    ("OpenDNS", "208.67.222.222", "208.67.220.220"),
    ("AdGuard", "94.140.14.14", "94.140.15.15"),
    ("CleanBrowsing", "185.228.168.9", "185.228.169.9"),
    ("Comodo Secure", "8.26.56.26", "8.20.247.20"),
    ("Level3", "4.2.2.1", "4.2.2.2"),
    ("Yandex", "77.88.8.8", "77.88.8.1"),
    ("Control D", "76.76.2.0", "76.76.10.0"),
    ("NextDNS", "45.90.28.0", "45.90.30.0"),
    ("DNS.SB", "185.222.222.222", "45.11.45.11"),
    ("Verisign", "64.6.64.6", "64.6.65.6"),
    ("Neustar", "156.154.70.1", "156.154.71.1"),
]
KNOWN_IPS = {a for _, a, _ in DNS_SERVERS}

# Android's "Private DNS" setting wants a hostname (DNS-over-TLS), not an IP.
PRIVATE_DNS_HOST = {
    "1.1.1.1": "one.one.one.one",
    "8.8.8.8": "dns.google",
    "9.9.9.9": "dns.quad9.net",
    "94.140.14.14": "dns.adguard-dns.com",
    "185.228.168.9": "security-filter-dns.cleanbrowsing.org",
    "185.222.222.222": "dot.sb",
}

TEST_DOMAINS = [
    "google.com", "youtube.com", "facebook.com", "wikipedia.org", "amazon.com",
    "microsoft.com", "cloudflare.com", "netflix.com", "instagram.com", "github.com",
]
SCAN_DOMAINS = ["google.com", "wikipedia.org", "microsoft.com"]

PUBLIC_DNS_URLS = [
    "https://public-dns.info/nameservers.csv",
    "https://public-dns.info/nameservers.txt",
]
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


class TestError(Exception):
    """A test could not run (message is shown to the user)."""


def _build_query(domain):
    tid = random.randint(0, 65535)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode() for p in domain.split(".")) + b"\x00"
    return tid, header + qname + struct.pack(">HH", 1, 1)


def dns_query_ms(server, domain, timeout=2.0):
    """Latency in ms of one UDP DNS query, or None on failure."""
    tid, pkt = _build_query(domain)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        t0 = time.perf_counter()
        s.sendto(pkt, (server, 53))
        data, _ = s.recvfrom(4096)
        dt = (time.perf_counter() - t0) * 1000.0
        if len(data) < 12:
            return None
        rid, flags = struct.unpack(">HH", data[:4])
        if rid != tid or (flags & 0x000F) != 0:  # id mismatch or RCODE != 0
            return None
        return dt
    except Exception:  # noqa: BLE001
        return None
    finally:
        s.close()


def measure_dns(ip, rounds, stop):
    """Latency / jitter / loss for one DNS server."""
    lat, diffs, sent = [], [], 0
    # Liveness pre-check: a dead / blocked server is reported in ~3 s instead of ~20 s.
    alive = False
    for _ in range(2):
        if stop.is_set():
            break
        if dns_query_ms(ip, TEST_DOMAINS[0], 1.5) is not None:
            alive = True
            break
        sent += 1
    if not alive:
        return dict(avg=math.inf, mn=math.inf, mx=math.inf, med=math.inf, jit=0.0,
                    loss=100.0, score=math.inf, sent=max(sent, 1), ok=0)
    sent = 0
    for domain in TEST_DOMAINS:
        if stop.is_set():
            break
        dns_query_ms(ip, domain)  # warm-up (fills the resolver cache), not counted
        series = []
        for _ in range(rounds):
            if stop.is_set():
                break
            sent += 1
            r = dns_query_ms(ip, domain)
            if r is not None:
                series.append(r)
        lat += series
        diffs += [abs(b - a) for a, b in zip(series, series[1:])]
        if sent >= 2 * rounds and not lat:  # dead server -> stop wasting time
            break
    ok = len(lat)
    if ok == 0:
        return dict(avg=math.inf, mn=math.inf, mx=math.inf, med=math.inf, jit=0.0,
                    loss=100.0, score=math.inf, sent=sent, ok=0)
    avg = statistics.mean(lat)
    med = statistics.median(lat)
    jit = statistics.mean(diffs) if diffs else 0.0
    loss = (sent - ok) / sent * 100.0 if sent else 100.0
    score = med + jit * 0.5 + loss * 10.0  # lower is better
    return dict(avg=avg, mn=min(lat), mx=max(lat), med=med, jit=jit, loss=loss,
                score=score, sent=sent, ok=ok)


def dns_label(r):
    return "{} [{}]".format(r["name"], r["cc"]) if r.get("cc") else r["name"]


def rank_dns(results):
    return sorted(results, key=lambda r: r["score"])


def best_dns(results):
    ranked = rank_dns(results)
    if ranked and not math.isinf(ranked[0]["score"]):
        return ranked[0]
    return None


def _valid_public_ipv4(text):
    if not IPV4_RE.match(text):
        return None
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return None
    if (ip.is_private or ip.is_loopback or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or ip.is_link_local):
        return None
    return str(ip)


def _col(row, *names):
    for n in names:
        v = row.get(n)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def parse_server_list(text, countries=None):
    """Parse a CSV (with header) or a plain list of IPs -> [{ip, name, cc, rel}] by reliability."""
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    if not lines:
        return []
    want = {c.strip().upper() for c in (countries or []) if c.strip()}
    out, seen = [], set()

    def add(ip_text, name="", cc="", rel=0.0):
        ip = _valid_public_ipv4(ip_text.strip())
        if not ip or ip in seen:
            return
        if want and cc.upper() not in want:
            return
        seen.add(ip)
        out.append(dict(ip=ip, name=name, cc=cc.upper(), rel=rel))

    first_cell = re.split(r"[,\s;]+", lines[0])[0]
    if "," in lines[0] and not IPV4_RE.match(first_cell):  # CSV with a header row
        reader = csv.DictReader(lines)
        reader.fieldnames = [(h or "").strip().lower() for h in (reader.fieldnames or [])]
        for row in reader:
            try:
                rel = float(_col(row, "reliability") or 0)
            except ValueError:
                rel = 0.0
            add(_col(row, "ip_address", "ip", "server", "address"),
                _col(row, "name", "as_org", "hostname"),
                _col(row, "country_code", "country_id", "country", "cc"), rel)
    else:  # plain list: first token on each line is the IP
        for l in lines:
            parts = re.split(r"[,\s;]+", l)
            cc = parts[1] if len(parts) > 1 and len(parts[1]) == 2 and parts[1].isalpha() else ""
            add(parts[0], "", cc)
    out.sort(key=lambda s: -s["rel"])
    return out


def _ssl_context():
    try:
        import certifi  # bundled in the APK; Android has no usable system CA path for Python
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001
        return ssl.create_default_context()


def fetch_online_servers(countries, timeout=25):
    last = "no data"
    ctx = _ssl_context()
    for url in PUBLIC_DNS_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "{}/{}".format(APP_NAME, APP_VERSION)})
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            servers = parse_server_list(raw, countries)
            if servers:
                return servers
            last = "{}: no matching servers".format(url)
        except Exception as exc:  # noqa: BLE001
            last = "{}: {}".format(url, exc)
    raise TestError("Could not download the public DNS list ({})".format(last))


def quick_scan(ip, stop, timeout=1.0):
    """Fast liveness check: 3 queries. Median latency (ms), or None if fewer than 2 answered."""
    lat = []
    for d in SCAN_DOMAINS:
        if stop.is_set():
            return None
        r = dns_query_ms(ip, d, timeout)
        if r is not None:
            lat.append(r)
    return statistics.median(lat) if len(lat) >= 2 else None


def run_dns_test(opts, stop, emit):
    """
    opts: source ('builtin'|'online'), countries [..], scan_max, top_n, rounds
    emit(kind, *args): 'log' msg | 'status' msg | 'progress' done total | 'dns_row' result
    Returns dict(results, scan_rows, stopped).
    """
    rounds = max(2, int(opts.get("rounds", 4)))
    builtin = [(n, a, b, "") for n, a, b in DNS_SERVERS]
    chosen, scan_rows = builtin, []

    if opts.get("source") == "online":
        emit("status", "Downloading the public DNS list...")
        lst = fetch_online_servers(opts.get("countries") or [])
        extra = [(s["name"] or s["ip"], s["ip"], "", s["cc"])
                 for s in lst[:int(opts.get("scan_max", 200))] if s["ip"] not in KNOWN_IPS]
        servers = builtin + extra
        emit("log", "Loaded {} servers; scanning {} (+{} built-in)".format(len(lst), len(extra), len(builtin)))
        emit("log", "Note: servers from open lists are run by unknown operators - "
                    "they can log or tamper with answers.")

        # ---- Stage 1: quick scan, many in parallel ----
        emit("log", "Stage 1: quick scan of {} servers...".format(len(servers)))
        alive, done = [], 0
        with ThreadPoolExecutor(max_workers=32) as ex:
            futs = {ex.submit(quick_scan, s[1], stop): s for s in servers}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    lat = fut.result()
                except Exception:  # noqa: BLE001
                    lat = None
                done += 1
                scan_rows.append((s, lat))
                if lat is not None:
                    alive.append((lat, s))
                if done % 8 == 0 or done == len(servers):
                    emit("progress", done, len(servers))
                    emit("status", "Quick scan {}/{} - {} responding".format(done, len(servers), len(alive)))
                if stop.is_set():
                    break
        if stop.is_set():
            return dict(results=[], scan_rows=scan_rows, stopped=True)
        if not alive:
            raise TestError("No server responded. Check your connection (UDP port 53 may be blocked on this network).")
        alive.sort(key=lambda x: x[0])
        top_n = max(3, int(opts.get("top_n", 15)))
        chosen = [s for _, s in alive[:top_n]]
        chosen += [s for _, s in alive[top_n:] if s[1] in KNOWN_IPS]  # keep known providers
        emit("log", "{} servers responded; fully testing {}".format(len(alive), len(chosen)))

    # ---- Stage 2: full test (latency / jitter / loss) ----
    emit("progress", 0, max(1, len(chosen)))
    emit("log", "Stage 2: {} servers x {} domains x {} rounds".format(len(chosen), len(TEST_DOMAINS), rounds))
    results, done = [], 0

    def one(entry):
        name, ip1, ip2, cc = entry
        m = measure_dns(ip1, rounds, stop)
        m.update(name=name, ip1=ip1, ip2=ip2, cc=cc)
        return m

    with ThreadPoolExecutor(max_workers=4) as ex:
        for fut in [ex.submit(one, e) for e in chosen]:
            try:
                r = fut.result()
            except Exception:  # noqa: BLE001
                continue
            if stop.is_set():
                break
            results.append(r)
            done += 1
            emit("progress", done, len(chosen))
            emit("status", "Tested {} ({}/{})".format(dns_label(r), done, len(chosen)))
            emit("dns_row", r)
    return dict(results=results, scan_rows=scan_rows, stopped=stop.is_set())


def save_dns_csv(results, scan_rows, stopped):
    """Write dns_results (+ dns_scan) CSVs and a history line. Returns (path, text) of the results file."""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if scan_rows:
        ordered = sorted(scan_rows, key=lambda x: (x[1] is None, x[1] or 0))
        rows = [[stamp, s[0], s[3], s[1], fnum(lat) if lat is not None else "",
                 "YES" if lat is not None else "NO"] for s, lat in ordered]
        write_csv("dns_scan", ["Timestamp", "Name", "Country", "IP",
                               "Quick latency ms (median of 3)", "Responded"], rows)
    ranked = rank_dns(results)
    best = best_dns(results)
    rows = []
    for i, r in enumerate(ranked, 1):
        rows.append([stamp, i, r["name"], r.get("cc", ""), r["ip1"], r["ip2"], fnum(r["avg"]), fnum(r["mn"]),
                     fnum(r["mx"]), fnum(r["med"]), fnum(r["jit"]), fnum(r["loss"], 0), fnum(r["score"]),
                     r["sent"], r["ok"], "YES" if best and r["ip1"] == best["ip1"] else ""])
    header = ["Timestamp", "Rank", "Provider", "Country", "Primary", "Secondary", "Latency avg ms",
              "Latency min ms", "Latency max ms", "Latency median ms", "Jitter ms", "Loss %", "Score",
              "Queries sent", "Queries ok", "Best"]
    path, text = write_csv("dns_results" + ("_partial" if stopped else ""), header, rows)
    if best:
        append_history("DNS", "{} {}/{}".format(best["name"], best["ip1"], best["ip2"]),
                       best["avg"], best["jit"], best["loss"], "partial" if stopped else "")
    return path, text


# ----------------------------------------------------------------------------
# MTU  (ping with the Don't-Fragment flag)
# ----------------------------------------------------------------------------
COMMON_MTUS = [1500, 1492, 1480, 1472, 1460, 1450, 1440, 1400, 1360, 1280]
MTU_FALLBACK_TARGETS = ["1.1.1.1", "8.8.8.8", "9.9.9.9", "208.67.222.222"]
MTU_TOLERANCE_MS = 2.0     # sizes within this score gap of the best are "equal"; the largest wins
DF_VARIANTS = [["-M", "do"], ["-D"]]   # Linux/Android toybox, then BSD-style ping
_TIME_RE = re.compile(r"time\s*[=<]\s*([\d.]+)\s*ms", re.I)


def find_ping():
    for p in ("/system/bin/ping", "/system/xbin/ping", "/bin/ping", "/usr/bin/ping"):
        if os.path.exists(p):
            return p
    return shutil.which("ping") or "ping"


def parse_ping_time(text):
    """RTT (ms) of the first reply line, or None."""
    for line in text.splitlines():
        m = _TIME_RE.search(line)
        if m and ("ttl" in line.lower() or "bytes from" in line.lower() or "from" in line.lower()):
            try:
                return float(m.group(1))
            except ValueError:
                return None
    return None


def ping_once(target, payload, df=None, timeout=2):
    """One echo request. Returns (ok, rtt_ms, raw_output)."""
    cmd = [find_ping(), "-c", "1", "-W", str(timeout)] + list(df or []) + ["-s", str(payload), target]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout + 4, stdin=subprocess.DEVNULL)
        out = ((p.stdout or b"") + (p.stderr or b"")).decode("utf-8", errors="ignore")
    except subprocess.TimeoutExpired:
        return False, None, "timeout"
    except Exception as exc:  # noqa: BLE001
        return False, None, "error: {}".format(exc)
    rtt = parse_ping_time(out)
    return rtt is not None, rtt, out


def _short(text, n=300):
    return " | ".join(l.strip() for l in text.splitlines() if l.strip())[:n] or "(empty)"


class Prober:
    """DF echo requests against one target."""

    def __init__(self, target):
        self.target = target
        self.df = None
        self.error = ""
        self.diag = []

    def select(self, stop, payload=32):
        """Find a ping variant that works, and check the phone really enforces Don't-Fragment."""
        self.error, self.diag = "", []
        for variant in DF_VARIANTS:
            if stop.is_set():
                return False
            ok, _, out = ping_once(self.target, payload, variant)
            if ok:
                self.df = variant
                ok_big, _, _ = ping_once(self.target, 20000, variant)  # must NOT get through
                if ok_big:
                    self.error = ("this phone's ping ignores the Don't-Fragment flag, "
                                  "so MTU cannot be measured reliably")
                    return False
                return True
            self.diag.append("ping {}: {}".format(" ".join(variant), _short(out)))
        self.error = "no reply from {}".format(self.target)
        return False

    def probe(self, payload, stop, tries=3):
        for _ in range(tries):
            if stop.is_set():
                return False
            if ping_once(self.target, payload, self.df)[0]:
                return True
        return False

    def series(self, payload, count, stop):
        lat = []
        for _ in range(count):
            if stop.is_set():
                break
            ok, rtt, _ = ping_once(self.target, payload, self.df)
            if ok and rtt is not None:
                lat.append(rtt)
            time.sleep(0.2)
        return lat


def choose_best_mtu(results):
    """Best = lowest score; among near-equal scores, the largest MTU."""
    valid = [r for r in results if not math.isinf(r["score"])]
    if not valid:
        return None
    best_score = min(r["score"] for r in valid)
    pool = [r for r in valid if r["score"] <= best_score + MTU_TOLERANCE_MS]
    return max(pool, key=lambda r: r["mtu"])


def run_mtu_test(target, count, stop, emit):
    """
    emit(kind, *args): 'log' msg | 'status' msg | 'progress' done total | 'target' ip |
                       'mtu_search' step payload ok | 'mtu_quality' result | 'mtu_best' result
    Returns dict(best, results, max_mtu, target, stopped). Raises TestError when it cannot run.
    """
    typed = (target or "").strip()
    try:
        ip = socket.gethostbyname(typed) if typed else None
    except OSError:
        emit("log", "Cannot resolve '{}' - using well-known targets instead.".format(typed))
        ip = None
    candidates = ([ip] if ip else []) + [t for t in MTU_FALLBACK_TARGETS if t != ip]

    # ---- Phase 1: largest MTU that passes without fragmentation ----
    prober = None
    for i, t in enumerate(candidates):
        p = Prober(t)
        if p.select(stop):
            prober = p
            break
        if stop.is_set():
            raise TestError("Stopped")
        if p.error.startswith("this phone"):
            raise TestError(p.error)
        if i == 0:
            for d in p.diag:
                emit("log", d)
            emit("log", "{} does not answer ping (many servers block ICMP) - trying well-known targets...".format(t))
    if prober is None:
        raise TestError("No reply from any target (ICMP blocked on this network, no internet, "
                        "or ping is unavailable on this phone). See the log.")
    if prober.target != ip:
        emit("log", "Using {} as the test target instead of {}".format(prober.target, typed))
    target = prober.target
    emit("target", target)
    emit("log", "Probe: ping {}".format(" ".join(prober.df)))

    step, base, top = 1, 32, 1472
    emit("mtu_search", step, base, True)
    step += 1
    ok = prober.probe(top, stop)
    emit("mtu_search", step, top, ok)
    if ok:
        lo = top
    else:
        lo, hi = base, top
        while hi - lo > 1 and not stop.is_set():
            mid = (lo + hi) // 2
            step += 1
            ok = prober.probe(mid, stop)
            emit("mtu_search", step, mid, ok)
            if ok:
                lo = mid
            else:
                hi = mid
            emit("progress", min(step, 12), 24)
            emit("status", "Searching... payload {}..{}".format(lo, hi))
    if stop.is_set():
        raise TestError("Stopped")
    max_mtu = lo + 28
    emit("log", "Largest working MTU = {}. Measuring latency / jitter per size...".format(max_mtu))

    # ---- Phase 2: latency / jitter / loss for the max size and common sizes below it ----
    cands = ([max_mtu] + [m for m in COMMON_MTUS if m < max_mtu])[:6]
    results = []
    for i, mtu in enumerate(cands):
        if stop.is_set():
            break
        emit("status", "Measuring MTU {} ({}/{})".format(mtu, i + 1, len(cands)))
        lat = prober.series(mtu - 28, count, stop)
        if stop.is_set():
            break
        if not lat:
            r = dict(mtu=mtu, avg=math.inf, mn=math.inf, mx=math.inf, jit=0.0, loss=100.0,
                     score=math.inf, sent=count, ok=0)
        else:
            avg, jit = statistics.mean(lat), jitter_of(lat)
            loss = (count - len(lat)) / count * 100.0
            r = dict(mtu=mtu, avg=avg, mn=min(lat), mx=max(lat), jit=jit, loss=loss,
                     score=avg + jit * 0.5 + loss * 10.0, sent=count, ok=len(lat))
        results.append(r)
        emit("mtu_quality", r)
        emit("progress", 12 + 12 * (i + 1) / len(cands), 24)

    best = choose_best_mtu(results)
    if best:
        emit("mtu_best", best)
    return dict(best=best, results=results, max_mtu=max_mtu, target=target, stopped=stop.is_set())


def save_mtu_csv(res):
    """Write mtu_results CSV + history line. Returns (path, text) or (None, "")."""
    results = res["results"]
    if not results:
        return None, ""
    best, stopped = res["best"], res["stopped"]
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for i, r in enumerate(sorted(results, key=lambda r: r["score"]), 1):
        rows.append([stamp, res["target"], i, r["mtu"], r["mtu"] - 28, fnum(r["avg"]), fnum(r["mn"]),
                     fnum(r["mx"]), fnum(r["jit"]), fnum(r["loss"], 0), fnum(r["score"]), r["sent"], r["ok"],
                     res["max_mtu"], "YES" if best and r["mtu"] == best["mtu"] else ""])
    header = ["Timestamp", "Target", "Rank", "MTU", "Payload", "Latency avg ms", "Latency min ms",
              "Latency max ms", "Jitter ms", "Loss %", "Score", "Pings sent", "Replies",
              "Max working MTU", "Best"]
    path, text = write_csv("mtu_results" + ("_partial" if stopped else ""), header, rows)
    if best:
        append_history("MTU", str(best["mtu"]), best["avg"], best["jit"], best["loss"],
                       "target {}; max working {}".format(res["target"], res["max_mtu"]) +
                       ("; partial" if stopped else ""))
    return path, text
