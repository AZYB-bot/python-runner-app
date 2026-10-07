import streamlit as st
import os
import re
import shutil
import tempfile
import random
import zipfile
import json
import time
import hmac
import threading
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from datetime import datetime
from collections import deque

try:
    import jmcomic
    from jmcomic import JmOption, create_option_by_str
    JMCOMIC_AVAILABLE = True
except ImportError:
    JMCOMIC_AVAILABLE = False

# set_page_config 必须是第一个 Streamlit 调用，所以放在 st.error 之前
st.set_page_config(page_title="JM Downloader", layout="wide")


# ── 兼容补丁：JM API 响应体开头多了 UTF-8 BOM ──────────────────────────
# 现象：禁漫的 API 域名(www.cdnhjk.net / www.cdngwc.cc / www.cdngwc.net …)
#       现在会在所有响应体最前面加 EF BB BF(即 \ufeff)。
#       jmcomic 的 api 客户端校验“第一个有效字符必须是 {”，只忽略
#       空格/换行/制表符，遇到 BOM 就判为“不是json格式”并无限重试，
#       最终抛 RequestRetryAllFailException，导致本应用所有功能全部失效。
# 做法：放宽该字符校验(忽略 BOM/零宽字符)，从而继续使用 api 客户端。
#       若补丁不适用(例如 jmcomic 内部结构调整)，自动退回 html 客户端。
def _install_bom_tolerance_patch() -> bool:
    try:
        import jmcomic.jm_client_impl as _impl
        from jmcomic import JmResp, JmcomicText, ExceptionTool
        from jmcomic.jm_config import JmModuleConfig

        cls = _impl.JmApiClient
        if getattr(cls, "_dsh_bom_patched", False):
            return True

        _abstract_orig = _impl.AbstractJmClient.raise_if_resp_should_retry
        _IGNORED = (" ", "\n", "\t", "\ufeff", "\u200b", "\u00a0")

        def _raise_if_resp_should_retry(self, resp, is_image):
            resp = _abstract_orig(self, resp, is_image)
            if isinstance(resp, JmResp):
                return resp

            code = resp.status_code
            if code >= 500:
                msg = JmModuleConfig.JM_ERROR_STATUS_CODE.get(code, f"HTTP状态码: {code}")
                ExceptionTool.raises_resp(f"禁漫API异常响应, {msg}", resp)

            url = resp.request.url
            if self.API_SCRAMBLE in url:
                return resp

            text = resp.text
            for char in text:
                if char not in _IGNORED:
                    ExceptionTool.require_true(
                        char == "{",
                        f"请求不是json格式，强制重试！响应文本: [{JmcomicText.limit_text(text, 200)}]",
                    )
                    return resp
            ExceptionTool.raises_resp(f"响应无数据！request_url=[{url}]", resp)

        cls.raise_if_resp_should_retry = _raise_if_resp_should_retry
        cls._dsh_bom_patched = True
        return True
    except Exception:
        return False


BOM_PATCHED = JMCOMIC_AVAILABLE and _install_bom_tolerance_patch()
CLIENT_IMPL = "api" if BOM_PATCHED else "html"

# 复用的客户端配置片段
CLIENT_YAML_3 = f"log: false\nclient: {{impl: {CLIENT_IMPL}, retry_times: 3}}"
CLIENT_YAML_5 = f"log: false\nclient: {{impl: {CLIENT_IMPL}, retry_times: 5}}"

try:
    from jmcomic.jm_config import JmModuleConfig
except Exception:
    JmModuleConfig = None


# ── 日志捕获：用 jmcomic 官方钩子，不做全局 stdout 重定向 ──────────────
# 旧写法 `sys.stdout = log_buffer` 有两个致命问题：
#   1. sys.stdout 是【进程级全局变量】，而 Streamlit 每个用户会话跑在各自
#      线程里。两个用户同时下载时，B 会把 A 的 StringIO 当成"原来的 stdout"
#      存下来，最后退出者又把它还原回去 —— 于是 A、B 的日志互相串台
#      （B 能看到 A 的下载日志），且 sys.stdout 被永久指向一个没人读的
#      缓冲区，此后整个进程的输出全部丢失。
#   2. 下载期间其它会话写入 stdout 的内容也会被吞进当前会话的缓冲区。
# 这里改用 jmcomic 的 EXECUTOR_LOG 钩子 + 线程本地缓冲区，天然按会话隔离。
_LOG_LOCAL = threading.local()


def _jm_log_sink(topic, msg, e=None):
    """jmcomic 日志出口：只写当前线程自己的捕获缓冲区，没有捕获时静默丢弃。"""
    if isinstance(msg, BaseException):
        e, msg = msg, str(msg)
    buf = getattr(_LOG_LOCAL, "buffer", None)
    if buf is not None:
        try:
            buf.write(f"【{topic}】{msg}\n")
        except Exception:
            pass


class _LogCapture:
    """线程本地的 jmcomic 日志捕获上下文，各会话互不干扰。"""

    def __enter__(self):
        self._buf = StringIO()
        self._prev = getattr(_LOG_LOCAL, "buffer", None)
        _LOG_LOCAL.buffer = self._buf
        # 注意：任何带 `log: false` 的 option 会调用 JmModuleConfig.disable_jm_log()，
        # 那是【进程级】开关。查询/搜索用过后日志就被全局关掉了，所以这里显式打开，
        # 否则下载日志面板永远是空的。
        if JmModuleConfig is not None:
            self._prev_flag = JmModuleConfig.FLAG_ENABLE_JM_LOG
            JmModuleConfig.FLAG_ENABLE_JM_LOG = True
        return self._buf

    def __exit__(self, *exc):
        _LOG_LOCAL.buffer = self._prev
        if JmModuleConfig is not None:
            JmModuleConfig.FLAG_ENABLE_JM_LOG = self._prev_flag
        return False

    @staticmethod
    def lines(buf) -> list:
        return [ln for ln in (buf.getvalue() or "").splitlines() if ln.strip()]


def _install_jm_log_sink() -> bool:
    if JmModuleConfig is None:
        return False
    try:
        JmModuleConfig.EXECUTOR_LOG = _jm_log_sink
        return True
    except Exception:
        return False


LOG_SINK_READY = _install_jm_log_sink()

if not JMCOMIC_AVAILABLE:
    st.error("⚠️ jmcomic 库未安装")
    st.stop()

# ── 配置 ──────────────────────────────────────────────
# 管理员密码只从 Streamlit Secrets 读取，不再硬编码进公开仓库。
# 部署端配置：Streamlit Cloud → 应用 → Settings → Secrets
#     ADMIN_PASSWORD = "你的密码"
def _load_admin_password() -> str:
    try:
        return str(st.secrets.get("ADMIN_PASSWORD", "") or "")
    except Exception:
        return ""


ADMIN_PASSWORD = _load_admin_password()
STATS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stats.json")

# ── 持久化统计 ────────────────────────────────────────
def _load_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {
        "total_requests": 0,
        "total_downloads": 0,
        "total_bytes": 0,
        "start_time": datetime.now().isoformat(),
        "download_logs": [],
    }

def _save_stats(stats):
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(stats, f, ensure_ascii=False)
    except Exception:
        pass

_stats = _load_stats()

def _log_request():
    _stats["total_requests"] += 1
    entry = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    _stats.setdefault("request_logs", []).insert(0, entry)
    if len(_stats["request_logs"]) > 200:
        _stats["request_logs"] = _stats["request_logs"][:200]
    _save_stats(_stats)

def _log_download(album_id, filename, size, status, ip=""):
    log_entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "album_id": album_id,
        "filename": filename,
        "size": size,
        "status": status,
        "ip": ip,
    }
    _stats["total_downloads"] += 1
    _stats["total_bytes"] += size
    _stats["download_logs"].insert(0, log_entry)
    if len(_stats["download_logs"]) > 200:
        _stats["download_logs"] = _stats["download_logs"][:200]
    _save_stats(_stats)

def fmt_bytes(b):
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b / 1024 / 1024:.1f} MB"

OPTION_BASE = {
    "download": {
        "cache": True,
        "image": {"decode": True, "suffix": ".jpg"},
        "threading": {"image": 20, "photo": 10},
    },
    "client": {"impl": CLIENT_IMPL, "retry_times": 3, "timeout": 15},
}

def _build_option(temp_dir: str) -> JmOption:
    cfg = dict(OPTION_BASE)
    cfg["dir_rule"] = {"rule": "Bd_Aid", "base_dir": temp_dir}
    cfg["plugins"] = {
        "after_photo": [
            {
                "plugin": "img2pdf",
                "kwargs": {
                    "pdf_dir": temp_dir,
                    "filename_rule": "Pindex",
                },
            }
        ]
    }
    return JmOption.construct(cfg)

# ── 数据获取层：统一加缓存，避免每次交互重复走网络 ────────────────────
# 约定：缓存函数用「抛异常」表示失败，只有成功结果才进缓存 —— 否则一次
#       网络抖动会被缓存住，用户接下来一小时都看到"失败"。
MAX_CHAPTER_PROBE = 200     # 单次最多并发探测多少个章节的页数


def _normalize_album_id(raw):
    """本子号必须是纯数字。提前拦掉非法输入，避免白跑 4 域名 x 3 次重试。"""
    m = re.fullmatch(r"\s*(\d{1,12})\s*", str(raw or ""))
    return m.group(1) if m else None


@st.cache_data(ttl=3600, show_spinner=False)
def _album_info_cached(album_id: str) -> dict:
    opt = create_option_by_str(CLIENT_YAML_3)
    client = opt.build_jm_client()
    album = client.get_album_detail(album_id)
    episodes = list(album.episode_list or [])

    authors = [a for a in (album.authors or []) if a != "N/A"]
    author = authors[0] if authors else (album.author or "")
    tags = [t for t in (album.tags or []) if t != "N/A"]

    def probe(ep):
        photo_id, _index, title = ep
        try:
            photo = client.get_photo_detail(photo_id)
            pages = len(photo.page_arr) if photo.page_arr else 0
            return {"id": photo_id, "title": photo.name or title or "", "pages": pages}
        except Exception:
            return {"id": photo_id, "title": title or "", "pages": 0}

    head = episodes[:MAX_CHAPTER_PROBE]
    if head:
        # 原来是 for 循环串行请求：实测 144 章要 318 秒 / 290 次串行 HTTP。
        # 各章节详情互不依赖，改用线程池并发（jmcomic 自身下载图片也是多线程的）。
        with ThreadPoolExecutor(max_workers=min(12, len(head))) as ex:
            chapters = list(ex.map(probe, head))
    else:
        chapters = []
    chapters += [
        {"id": pid, "title": t or "", "pages": 0}
        for pid, _i, t in episodes[MAX_CHAPTER_PROBE:]
    ]

    return {
        "id": album.album_id,
        "title": album.name,
        "author": author,
        "tags": tags,
        "chapter_count": len(episodes),
        "page_count": sum(c["pages"] for c in chapters),
        "chapters": chapters,
        "partial": len(episodes) > MAX_CHAPTER_PROBE,
    }


def get_album_info(album_id: str):
    aid = _normalize_album_id(album_id)
    if aid is None:
        return {"error": "本子号必须是数字，例如 422866"}
    try:
        return _album_info_cached(aid)
    except Exception as e:
        return {"error": str(e)}

@st.cache_data(ttl=600, show_spinner=False)
def _search_tag_cached(tag: str, page: int) -> list:
    opt = create_option_by_str(CLIENT_YAML_3)
    client = opt.build_jm_client()
    result = client.search_tag(tag, page=page)
    return [{"id": aid, "title": title} for aid, title in result.iter_id_title()]


def search_tag(tag: str, page: int = 1):
    try:
        items = _search_tag_cached(str(tag).strip(), int(page))
        return {"items": items, "page": page, "tag": tag}
    except Exception as e:
        return {"error": str(e)}

def random_album(tag: str = "百合"):
    try:
        # 随机页仍然随机，但同一 (标签, 页码) 的搜索结果会被缓存复用
        page = random.randint(1, 30)
        items = _search_tag_cached(f"+{tag}", page)
        if not items:
            return {"error": f"标签「{tag}」无结果"}
        item = random.choice(items)
        return {"id": item["id"], "title": item["title"], "tag": tag}
    except Exception as e:
        return {"error": str(e)}

@st.cache_data(ttl=3600, show_spinner=False)
def _cover_cached(album_id: str) -> bytes:
    opt = JmOption.construct({
        "log": False,
        "client": {"impl": CLIENT_IMPL, "retry_times": 5},
        "download": {"cache": False, "image": {"suffix": ".jpg"}},
    })
    client = opt.build_jm_client()
    album = client.get_album_detail(album_id)
    if not album.episode_list:
        raise RuntimeError("该本子没有章节")

    photo = client.get_photo_detail(album.episode_list[0][0])
    if not photo.page_arr:
        raise RuntimeError("首个章节没有图片")

    img_detail = photo.create_image_detail(0)
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        client.download_by_image_detail(img_detail, path)
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def get_cover_image(album_id: str):
    """封面图。原实现无缓存，实测每次要 7 秒 / 4~6 次 HTTP，而它在每次
    rerun 都会被调用两次（本子详情 + 每日推荐）—— 加了缓存后基本归零。"""
    aid = _normalize_album_id(album_id)
    if aid is None:
        return None
    try:
        return _cover_cached(aid)
    except Exception:
        return None

@st.cache_data(ttl=1800, show_spinner=False, max_entries=4)
def _page_images_cached(album_id: str, max_pages: int) -> list:
    opt = JmOption.construct({
        "log": False,
        "client": {"impl": CLIENT_IMPL, "retry_times": 5},
        "download": {"cache": False, "image": {"suffix": ".jpg"}},
    })
    client = opt.build_jm_client()
    album = client.get_album_detail(album_id)
    if not album.episode_list:
        raise RuntimeError("该本子没有章节")

    images = []
    count = 0
    for photo_id, _, _ in album.episode_list:
        if count >= max_pages:
            break
        try:
            photo = client.get_photo_detail(photo_id)
            if not photo.page_arr:
                continue
            for i in range(len(photo.page_arr)):
                if count >= max_pages:
                    break
                img_detail = photo.create_image_detail(i)
                fd, path = tempfile.mkstemp(suffix=".jpg")
                os.close(fd)
                try:
                    client.download_by_image_detail(img_detail, path)
                    with open(path, "rb") as f:
                        images.append(f.read())
                finally:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                count += 1
        except Exception:
            continue
    if not images:
        raise RuntimeError("未获取到任何预览图")
    return images


def get_page_images(album_id: str, max_pages: int = 20):
    """获取本子前几页图片用于预览。max_entries=4 限制缓存体积(每份最多 20 张图)。"""
    aid = _normalize_album_id(album_id)
    if aid is None:
        return []
    try:
        return _page_images_cached(aid, int(max_pages))
    except Exception:
        return []

def get_top_album():
    """获取全站排行榜第一的本子"""
    today = datetime.now().strftime("%Y%m%d")
    cache = st.session_state.get('daily_top')
    if cache and cache.get('date') == today:
        return cache

    for tag in ("全彩", "百合", "人妻"):
        try:
            items = _search_tag_cached(f"+{tag}", 1)
        except Exception:
            continue
        if items:
            rec = {"date": today, "id": items[0]["id"], "title": items[0]["title"]}
            st.session_state['daily_top'] = rec
            return rec
    return None

# ── 下载：落盘 + 延迟读取，避免大文件 OOM ──────────────────────────────
# 旧实现把整包读成 bytes 塞进 Python 内存再交给 st.download_button；批量
# 下载更是一次性把 N 个 PDF 全留在内存里、再在内存里拼 zip —— 大本子必然
# 打爆 Streamlit Cloud 的内存。新实现全程走磁盘：
#   * 每个本子的产物落到临时目录，只把【路径】往下传
#   * zip 用 zf.write(磁盘文件) 而不是 writestr(内存 bytes)
#   * st.download_button(data=lambda: open(path,'rb')) 走 Streamlit 的延迟
#     下载通道(add_deferred)：只有用户真的点下载时才读盘
_DL_ROOT = os.path.join(tempfile.gettempdir(), "jm_downloads")
_DL_MAX_AGE = 6 * 3600      # 超过 6 小时的下载产物由下次运行时清掉


def _cleanup_stale_downloads():
    """清理过期的下载产物，避免临时目录无限增长。"""
    try:
        if not os.path.isdir(_DL_ROOT):
            return
        now = time.time()
        for name in os.listdir(_DL_ROOT):
            path = os.path.join(_DL_ROOT, name)
            try:
                if now - os.path.getmtime(path) > _DL_MAX_AGE:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass
    except Exception:
        pass


def _discard_download(entry):
    """删除某个下载产物目录(开始新下载时清掉上一份)。"""
    if not entry:
        return
    d = entry.get("dir")
    if d and os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)


def download_album_to_disk(album_id: str):
    """下载本子并【保留】产物在磁盘上，返回路径供延迟下载使用。"""
    aid = _normalize_album_id(album_id) or str(album_id)
    os.makedirs(_DL_ROOT, exist_ok=True)
    temp_dir = tempfile.mkdtemp(prefix=f"jm_{aid}_", dir=_DL_ROOT)

    # 日志走线程本地捕获，不再动 sys.stdout —— 见 _LogCapture 的说明
    with _LogCapture() as buf:
        try:
            option = _build_option(temp_dir)
            option.download_album(aid)

            pdf_files = sorted(
                [f for f in os.listdir(temp_dir) if f.endswith(".pdf")],
                key=lambda x: int(re.sub(r"\D", "", x) or 0),
            )
            if not pdf_files:
                raise RuntimeError("未生成 PDF 文件")

            if len(pdf_files) == 1:
                final_path = os.path.join(temp_dir, f"{aid}.pdf")
                os.replace(os.path.join(temp_dir, pdf_files[0]), final_path)
                filename = f"{aid}.pdf"
            else:
                final_path = os.path.join(temp_dir, f"{aid}.zip")
                with zipfile.ZipFile(final_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for pf in pdf_files:
                        # 从磁盘流式写入，不再需要把 PDF 读进内存
                        zf.write(os.path.join(temp_dir, pf), pf)
                filename = f"{aid}.zip"

            size = os.path.getsize(final_path)
            _log_download(aid, filename, size, "已完成")
            return {
                "status": "done",
                "path": final_path,
                "dir": temp_dir,
                "filename": filename,
                "size": size,
                "logs": _LogCapture.lines(buf),
            }
        except Exception as e:
            _log_download(aid, "", 0, "失败")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return {"status": "error", "message": str(e), "logs": _LogCapture.lines(buf)}


def _download_button(entry, label, key):
    """渲染延迟下载按钮：只有用户点击时才真正读盘。"""
    if not entry or entry.get("status") != "done":
        return
    path = entry.get("path")
    if not path or not os.path.exists(path):
        return
    st.download_button(
        label=f"{label} ({fmt_bytes(entry['size'])})",
        data=lambda p=path: open(p, "rb"),
        file_name=entry["filename"],
        mime="application/pdf" if entry["filename"].endswith(".pdf") else "application/zip",
        key=key,
    )


# ── 记录请求 ──────────────────────────────────────────
_cleanup_stale_downloads()
_log_request()

# ── UI ────────────────────────────────────────────────
st.title("📚 JM Downloader")

tab1, tab2, tab3, tab4 = st.tabs(["本子下载", "搜索", "管理面板", "批量下载"])

with tab1:
    col1, col2, col3 = st.columns([4, 2, 2])
    
    with col1:
        album_id = st.text_input("输入本子号", placeholder="如 422866")
    
    with col2:
        st.write("")
        st.write("")
        search_btn = st.button("查询")
    
    with col3:
        st.write("")
        st.write("")
        random_lily = st.button("🎲 随机百合")
        random_guro = st.button("🎲 随机猎奇")

    if random_lily:
        result = random_album("百合")
        if "error" not in result:
            st.session_state['temp_album_id'] = result["id"]
            st.success(f"🎲 随机百合: [{result['id']}] {result['title']}")
            st.session_state['temp_album_info'] = get_album_info(result["id"])

    if random_guro:
        result = random_album("猎奇")
        if "error" not in result:
            st.session_state['temp_album_id'] = result["id"]
            st.success(f"🎲 随机猎奇: [{result['id']}] {result['title']}")
            st.session_state['temp_album_info'] = get_album_info(result["id"])

    if search_btn and album_id:
        info = get_album_info(album_id)
        if "error" in info:
            st.error(f"获取失败: {info['error']}")
        else:
            st.session_state['temp_album_info'] = info

    info = st.session_state.get('temp_album_info')
    if info and "error" not in info:
        col_left, col_right = st.columns([3, 7])
        with col_left:
            cover_data = get_cover_image(info["id"])
            if cover_data:
                st.image(cover_data)
            else:
                st.image("https://via.placeholder.com/160x220?text=No+Cover")

        with col_right:
            st.subheader(f"[{info['id']}] {info['title']}")
            st.write(f"**作者:** {info['author']}")
            st.write(f"**章节数:** {info['chapter_count']}")
            st.write(f"**总页数:** {info['page_count']}")

            if info["tags"]:
                st.write("**标签:**")
                tags_str = ", ".join(info["tags"])
                st.write(f"{tags_str}")

            st.write("**章节列表:**")
            for i, ch in enumerate(info["chapters"], 1):
                st.write(f"{i}. {ch['title']} ({ch['pages']}页)")

            col_btn1, col_btn2 = st.columns(2)
            with col_btn1:
                dl_clicked = st.button("开始下载 PDF")
            with col_btn2:
                preview_clicked = st.button("在线预览 (前20页)")

            if dl_clicked:
                progress_bar = st.progress(0)
                status_text = st.empty()

                status_text.text(f"正在下载本子 {info['id']}...")

                # 先清掉上一次的产物，避免临时目录堆积
                _discard_download(st.session_state.get('last_download'))
                st.session_state.pop('last_download', None)

                result = download_album_to_disk(info["id"])

                progress_bar.progress(100)

                if result["status"] == "done":
                    st.session_state['last_download'] = result
                    status_text.text(f"✅ 下载完成！（{fmt_bytes(result['size'])}）")
                else:
                    status_text.text("❌ 下载失败")
                    st.error(result["message"])

                if result["logs"]:
                    with st.expander(f"下载日志（{len(result['logs'])} 行）", expanded=False):
                        st.text("\n".join(result["logs"]))

            # 下载按钮跨 rerun 保留；data 传 callable，点击时才读盘
            _download_button(st.session_state.get('last_download'), "⬇️ 下载文件", "dl_single")

            if preview_clicked:
                with st.status("正在加载预览图...", expanded=True):
                    images = get_page_images(info["id"], max_pages=20)
                    if images:
                        for idx, img in enumerate(images, 1):
                            st.image(img, caption=f"第 {idx} 页")
                    else:
                        st.error("加载预览图失败")

with tab2:
    col1, col2 = st.columns([4, 1])
    with col1:
        tag = st.text_input("输入标签", placeholder="如：百合、全彩、人妻")
    with col2:
        st.write("")
        st.write("")
        search_tag_btn = st.button("搜索")

    if search_tag_btn and tag:
        result = search_tag(tag, page=1)
        if "error" in result:
            st.error(f"搜索失败: {result['error']}")
        else:
            st.session_state['search_result'] = result

    search_result = st.session_state.get('search_result')
    if search_result:
        if search_result["items"]:
            for item in search_result["items"]:
                if st.button(f"[{item['id']}] {item['title']}", key=f"search_{item['id']}"):
                    info = get_album_info(item["id"])
                    if "error" not in info:
                        st.session_state['temp_album_info'] = info
        else:
            st.write("无结果")

with tab3:
    # 密码验证
    admin_verified = st.session_state.get('admin_verified', False)

    if not ADMIN_PASSWORD:
        # 不再提供任何内置默认密码：原来硬编码在公开仓库里的 "dahan123"
        # 等于把管理面板对所有能搜到仓库的人开放。
        st.subheader("🔒 管理员登录")
        st.warning(
            "尚未配置管理员密码，管理面板已锁定。\n\n"
            "请在 **Streamlit Cloud → 你的应用 → Settings → Secrets** 中添加：\n\n"
            "```toml\nADMIN_PASSWORD = \"你的密码\"\n```\n\n"
            "保存后应用会自动重启，即可使用。"
        )
    elif not admin_verified:
        st.subheader("🔒 管理员登录")
        with st.form("admin_login", clear_on_submit=True):
            pwd = st.text_input("请输入管理员密码", type="password")
            submitted = st.form_submit_button("登录")
        if submitted:
            # compare_digest 做定长时间比较；失败计数稍微拖慢暴力破解
            if ADMIN_PASSWORD and hmac.compare_digest(str(pwd or ""), ADMIN_PASSWORD):
                st.session_state['admin_verified'] = True
                st.session_state['admin_fails'] = 0
                st.success("登录成功！")
                st.rerun()
            else:
                fails = st.session_state.get('admin_fails', 0) + 1
                st.session_state['admin_fails'] = fails
                if fails >= 3:
                    time.sleep(min(fails, 10) * 0.5)
                st.error("密码错误")
    else:
        # 已登录 - 显示管理面板
        st.subheader("📊 管理面板")
        
        if st.button("退出登录"):
            st.session_state['admin_verified'] = False
            st.rerun()
        
        # 流量概览
        st.markdown("### 流量概览")
        col1, col2, col3, col4 = st.columns(4)
        start = datetime.fromisoformat(_stats["start_time"])
        uptime = int((datetime.now() - start).total_seconds())
        
        with col1:
            st.metric("总请求数", _stats["total_requests"])
        with col2:
            st.metric("总下载数", _stats["total_downloads"])
        with col3:
            st.metric("总流量", fmt_bytes(_stats["total_bytes"]))
        with col4:
            h = uptime // 3600
            m = (uptime % 3600) // 60
            st.metric("运行时间", f"{h}h{m}m")
        
        # 下载任务列表
        st.markdown("### 📥 下载任务")
        logs = _stats.get("download_logs", [])
        if logs:
            table_data = []
            for log in logs[:50]:
                table_data.append([
                    log["time"],
                    log["album_id"],
                    log["filename"],
                    fmt_bytes(log["size"]),
                    log["status"],
                ])
            st.dataframe(
                table_data,
                column_config={
                    "0": "时间",
                    "1": "本子号",
                    "2": "文件名",
                    "3": "大小",
                    "4": "状态",
                },
                width="stretch",
                hide_index=True,
                height=400,
            )
        else:
            st.info("暂无下载记录")
        
        # 请求日志
        st.markdown("### 🌐 请求日志")
        st.write(f"自 {start.strftime('%Y-%m-%d %H:%M:%S')} 启动以来，共处理 {_stats['total_requests']} 次请求。")
        
        req_logs = _stats.get("request_logs", [])
        if req_logs:
            req_data = [[i + 1, log["time"]] for i, log in enumerate(req_logs[:50])]
            st.dataframe(
                req_data,
                column_config={
                    "0": "#",
                    "1": "访问时间",
                },
                width="stretch",
                hide_index=True,
                height=300,
            )
        else:
            st.info("暂无请求记录")
        
        # 清除统计
        if st.button("🗑️ 清除所有统计"):
            _stats.update({
                "total_requests": 0,
                "total_downloads": 0,
                "total_bytes": 0,
                "start_time": datetime.now().isoformat(),
                "download_logs": [],
                "request_logs": [],
            })
            _save_stats(_stats)
            st.success("统计已清除！")
            st.rerun()

with tab4:
    st.subheader("📦 批量下载")
    st.write("输入多个本子号，每行一个或用逗号分隔")
    
    batch_input = st.text_area("输入本子号列表", placeholder="422866\n422867\n422868\n或: 422866, 422867, 422868", height=120)
    
    if st.button("开始批量下载", key="batch_dl"):
        # 解析输入
        ids = re.split(r'[\n,，\s]+', batch_input.strip())
        ids = [i.strip() for i in ids if i.strip()]

        if not ids:
            st.error("请输入至少一个本子号")
        else:
            st.info(f"共 {len(ids)} 个本子，开始批量下载...")

            _discard_download(st.session_state.get('last_batch'))
            st.session_state.pop('last_batch', None)

            ok_entries = []
            failed = []

            progress_bar = st.progress(0)
            status_text = st.empty()

            for idx, aid in enumerate(ids):
                status_text.text(f"[{idx + 1}/{len(ids)}] 正在下载 {aid}...")

                result = download_album_to_disk(aid)

                if result["status"] == "done":
                    # 只留路径，PDF 本体一直在磁盘上，不进内存
                    ok_entries.append(result)
                    st.write(f"✅ {aid} 下载完成（{fmt_bytes(result['size'])}）")
                else:
                    failed.append(aid)
                    st.write(f"❌ {aid} 下载失败: {result['message']}")

                progress_bar.progress((idx + 1) / len(ids))

            # 打包：从磁盘逐个流式写入 zip，而不是在内存里拼 bytes
            if ok_entries:
                os.makedirs(_DL_ROOT, exist_ok=True)
                batch_dir = tempfile.mkdtemp(prefix="jm_batch_", dir=_DL_ROOT)
                zip_path = os.path.join(batch_dir, f"batch_{len(ok_entries)}albums.zip")
                try:
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for e in ok_entries:
                            zf.write(e["path"], e["filename"])
                finally:
                    # 单本产物已经进了 zip，及时删掉释放磁盘
                    for e in ok_entries:
                        _discard_download(e)

                entry = {
                    "status": "done",
                    "path": zip_path,
                    "dir": batch_dir,
                    "filename": os.path.basename(zip_path),
                    "size": os.path.getsize(zip_path),
                }
                st.session_state['last_batch'] = entry

                status_text.text(f"✅ 批量下载完成！（{fmt_bytes(entry['size'])}）")

                summary = f"成功 {len(ok_entries)} 个"
                if failed:
                    summary += f"，失败 {len(failed)} 个: {', '.join(failed)}"
                st.write(summary)
            else:
                status_text.text("❌ 全部下载失败")
                st.error("没有成功下载任何本子")

    _download_button(st.session_state.get('last_batch'), "📦 下载全部文件", "dl_batch")

# ── 每日推荐 ──────────────────────────────────────────
st.markdown("---")
st.subheader("🌟 每日推荐")
top = get_top_album()
if top:
    col_a, col_b = st.columns([1, 8])
    with col_a:
        cover = get_cover_image(top["id"])
        if cover:
            st.image(cover, width=120)
    with col_b:
        st.write(f"**本子号:** {top['id']}")
        st.write(f"**标题:** {top['title']}")
        if st.button("查看详情", key="daily_rec_btn"):
            info = get_album_info(top["id"])
            if "error" not in info:
                st.session_state['temp_album_info'] = info
                st.rerun()
else:
    st.info("获取每日推荐失败，请稍后再试")