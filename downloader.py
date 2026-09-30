"""Phigros谱面在线下载源

支持多镜像源(国内可切换):
  - github:   原始 GitHub raw (raw.githubusercontent.com)
  - jsdelivr: jsDelivr CDN (cdn.jsdelivr.net/gh/...@main)
  - ghproxy: ghproxy.net 代理 (mirror.ghproxy.com 或 gh-proxy.com)

谱面实际路径:
  Chart_info.json (索引)
  chart/<DirId>.0/<DIFF>.json (DirId 由 SongId 经过"文件系统安全化"得到)
只下载指定难度的谱面JSON。
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
from typing import Callable

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


INDEX_CACHE_FILE = './cache/dl_song_index.json'  # 索引缓存到磁盘,避免每次启动都下载

# ---- 数据源定义 ----
SOURCES = {
    'github': {
        'label': 'GitHub (海外)',
        'raw_base': 'https://raw.githubusercontent.com/Dehou23333-awa/PhiResources/main/',
        'api_base': 'https://api.github.com/repos/Dehou23333-awa/PhiResources/contents/',
    },
    'jsdelivr': {
        'label': 'jsDelivr CDN (国内推荐)',
        'raw_base': 'https://cdn.jsdelivr.net/gh/Dehou23333-awa/PhiResources@main/',
        'api_base': None,  # jsDelivr 不提供目录列表 API,走 GitHub API 兜底
    },
    'ghproxy': {
        'label': 'GHProxy (国内备用)',
        'raw_base': 'https://mirror.ghproxy.com/https://raw.githubusercontent.com/Dehou23333-awa/PhiResources/main/',
        'api_base': 'https://mirror.ghproxy.com/https://api.github.com/repos/Dehou23333-awa/PhiResources/contents/',
    },
    'kkgithub': {
        'label': 'KKGitHub (国内备用)',
        'raw_base': 'https://kkgithub.com/Dehou23333-awa/PhiResources/raw/main/',
        'api_base': None,
    },
}

_DIFFICULTIES = ('EZ', 'HD', 'IN', 'AT', 'SP')
DEFAULT_SOURCE = 'jsdelivr'


def _dir_candidates(song_id: str) -> list[str]:
    cands = set()
    s = song_id
    cands.add(s)
    s1 = re.sub(r'[\s,，\.。]', '', s)
    cands.add(s1)
    s2 = re.sub(r'\bvs\.?\s*', 'vs', s, flags=re.IGNORECASE)
    s2 = re.sub(r'[\s,，\.。]', '', s2)
    cands.add(s2)
    s3 = re.sub(r'\s+', '', song_id)
    cands.add(s3)
    return [c for c in cands if c]


def _norm(x: str) -> str:
    return re.sub(r'[^a-z0-9]', '', x.lower())


class Downloader:
    def __init__(self, tracks_dir: str = './Assets/Tracks', source: str = DEFAULT_SOURCE) -> None:
        self.tracks_dir = tracks_dir
        self.source = source if source in SOURCES else DEFAULT_SOURCE
        self.song_index: dict[str, dict] = {}
        self._dir_index: dict[str, str] = {}
        self._all_dirs: list[str] = []
        self._loaded = False
        # source      = 用户在界面上选的源(权威, 任何自动逻辑都不许改)
        # source_used = 上一次实际把数据传过来的源(仅用于状态显示)
        # _dirs_source= 上一次实际列出目录用的源(jsDelivr没有目录API, 要借github的)
        self.source_used: str = self.source
        self._dirs_source: str = self.source

    def has_cached_index(self) -> bool:
        return os.path.exists(INDEX_CACHE_FILE)

    def set_source(self, source: str) -> None:
        if source in SOURCES and source != self.source:
            self.source = source
            self.source_used = source
            # 切源后重新拉: 目录缓存和"已加载"标记都清掉
            self._all_dirs = []
            self._dir_index = {}
            self._loaded = False

    @property
    def src(self):
        return SOURCES[self.source]

    def _get(self, url: str, timeout: int = 20, **kwargs):
        headers = kwargs.pop('headers', {})
        headers.setdefault('User-Agent', 'phisap-downloader/1.0')
        r = requests.get(url, timeout=timeout, headers=headers, **kwargs)
        r.raise_for_status()
        return r

    def _save_index_cache(self) -> None:
        try:
            os.makedirs(os.path.dirname(INDEX_CACHE_FILE), exist_ok=True)
            with open(INDEX_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump({'source': self.source, 'Songs': self.song_index}, f, ensure_ascii=False)
        except Exception:
            pass

    def _load_index_cache(self) -> bool:
        """尝试从磁盘加载缓存,成功返回True"""
        try:
            if not os.path.exists(INDEX_CACHE_FILE):
                return False
            with open(INDEX_CACHE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
            songs = data.get('Songs')
            if isinstance(songs, dict) and songs:
                self.song_index = songs
                # 注意: 这里**不能**改写 self.source。
                # 缓存里记的只是"当初这份数据是谁给的", 而用户在界面上选的源才是权威,
                # 之前的版本在这里把 source 覆盖成缓存里的值, 导致用户的源选择永远失效。
                saved_src = data.get('source')
                if saved_src in SOURCES:
                    self.source_used = saved_src
                self._loaded = True
                return True
        except Exception:
            pass
        return False

    def clear_index_cache(self) -> bool:
        """清除磁盘缓存,返回是否有文件被删"""
        self._loaded = False
        self.song_index = {}
        self._all_dirs = []
        self._dir_index = {}
        self.source_used = self.source
        try:
            if os.path.exists(INDEX_CACHE_FILE):
                os.remove(INDEX_CACHE_FILE)
                return True
        except Exception:
            pass
        return False

    def load_index(self, force: bool = False, use_cache: bool = True) -> None:
        if requests is None:
            raise RuntimeError('需要先安装 requests: pip install requests')
        if self._loaded and not force:
            return
        if not force and use_cache and self._load_index_cache():
            return
        last_err = None
        for sname in self._raw_candidates():
            url = SOURCES[sname]['raw_base'] + 'Chart_info.json'
            try:
                r = self._get(url, timeout=20)
                self.song_index = r.json().get('Songs', {})
                self.source_used = sname      # 只记录实际来源, 不改用户选择
                self._loaded = True
                self._save_index_cache()
                return
            except Exception as e:
                last_err = e
                continue
        # 所有源都失败,若有缓存则退回用缓存
        if self._load_index_cache():
            return
        raise RuntimeError(f'所有镜像源都无法连接且无本地缓存: {last_err}')

    def _raw_candidates(self) -> list[str]:
        '''下载时的源尝试顺序: 用户选的源永远排第一, 其余按字典序兜底'''
        return [self.source] + [s for s in SOURCES if s != self.source]

    def _api_candidates(self) -> list[str]:
        '''列目录时的API尝试顺序(jsDelivr/kkgithub没有目录API, 只能借github/ghproxy的)'''
        return [self.source] + [s for s in SOURCES if s != self.source]

    def _load_dirs(self) -> None:
        if self._all_dirs:
            return
        last_err = None
        # 注意: 这里也**不能**改 self.source。
        # jsDelivr / kkgithub 没有目录列表API, 只能借 github / ghproxy 的 API 列目录,
        # 但"列目录"和"下载谱面"是两件事: 目录列表是同一个仓库的内容, 与用哪个源无关,
        # 而下载必须回到用户选的源。之前的版本在这里把 source 改成 github,
        # 于是用户选了 jsDelivr 之后, 下载还是走 raw.githubusercontent.com。
        for sname in self._api_candidates():
            api = SOURCES[sname].get('api_base')
            if not api:
                continue
            try:
                r = self._get(api + 'chart', timeout=20)
                items = r.json()
                dirs = [it['name'] for it in items if it.get('type') == 'dir']
                if dirs:
                    self._all_dirs = dirs
                    self._dirs_source = sname   # 仅记录, 不影响下载用的源
                    return
            except Exception as e:
                last_err = e
                continue
        # 所有源都不通时,尝试从raw地址下载一个已知的文本列表不现实——
        # 返回空列表,后续 _resolve_dir 会尝试直接拼接URL
        if last_err:
            raise RuntimeError(f'无法获取仓库目录列表: {last_err}')

    def _raw_url(self, rel_path: str) -> str:
        return self.src['raw_base'] + rel_path

    def _try_raw_download(self, rel_path: str):
        """尝试从raw地址下载,返回Response或抛异常"""
        return self._get(self._raw_url(rel_path), timeout=30)

    def _resolve_dir(self, song_id: str) -> str:
        """返回仓库 chart/ 下对应的目录名(含.0后缀)。优先用缓存的目录列表;
        若目录列表不可用,直接尝试多个候选URL(能下成功就使用)"""
        if song_id in self._dir_index:
            return self._dir_index[song_id]
        try:
            self._load_dirs()
        except Exception:
            self._all_dirs = []

        if self._all_dirs:
            for cand in _dir_candidates(song_id):
                for suffix in ('.0', ''):
                    name = cand + suffix
                    if name in self._all_dirs:
                        self._dir_index[song_id] = name
                        return name
            low_id = song_id.lower()
            for d in self._all_dirs:
                base = d[:-2] if d.endswith('.0') else d
                if (base.lower() == low_id
                        or re.sub(r'[\s,，\.。]', '', base).lower() == re.sub(r'[\s,，\.。]', '', low_id)):
                    self._dir_index[song_id] = d
                    return d
            nid = _norm(song_id)
            for d in self._all_dirs:
                base = d[:-2] if d.endswith('.0') else d
                if _norm(base) == nid:
                    self._dir_index[song_id] = d
                    return d

        # 目录列表不可用,尝试直接拼URL(HEAD探测):
        for cand in _dir_candidates(song_id):
            for suffix in ('.0', ''):
                name = cand + suffix
                test_url = self._raw_url(f'chart/{urllib.parse.quote(name, safe="")}/AT.json')
                try:
                    requests.head(test_url, timeout=8).raise_for_status()
                    self._dir_index[song_id] = name
                    return name
                except Exception:
                    continue
        raise RuntimeError(f'在仓库中未找到 {song_id} 对应的目录')

    def search(self, keyword: str, case_sensitive: bool = False) -> list[tuple[str, str, str, list[str]]]:
        kw = keyword.strip()
        if not case_sensitive:
            kw = kw.lower()
        results = []
        for sid, info in self.song_index.items():
            title = info.get('Name', '')
            composer = info.get('Composer', '')
            diffs = [d for d in _DIFFICULTIES if d in info]
            if kw:
                if case_sensitive:
                    if kw not in sid and kw not in title and kw not in composer:
                        continue
                else:
                    if kw not in sid.lower() and kw not in title.lower() and kw not in composer.lower():
                        continue
            results.append((sid, title, composer, diffs))
        results.sort(key=lambda x: x[1].lower())
        return results

    def download_chart(
        self,
        song_id: str,
        diff: str,
        on_progress: Callable[[str], None] | None = None,
        on_done: Callable[[bool, str, str | None], None] | None = None,
    ) -> None:
        def say(m):
            if on_progress:
                on_progress(m)
        try:
            if not self._loaded:
                self.load_index()
            info = self.song_index.get(song_id)
            if not info:
                raise RuntimeError(f'未在索引中找到 {song_id}')
            if diff not in info:
                raise RuntimeError(f'{info.get("Name", song_id)} 无 {diff} 难度')

            say(f'查找仓库目录(源: {self.src["label"]})...')
            dir_name = self._resolve_dir(song_id)
            enc_dir = urllib.parse.quote(dir_name, safe='')
            filename = f'{diff}.json'
            rel = f'chart/{enc_dir}/{filename}'
            say(f'下载 {info.get("Name", song_id)} [{diff}] from {self.src["label"]}...')

            data = None
            last_err = None
            # 用户选的源排第一; 失败才依次兜底。兜底只记 source_used, 不改用户选择。
            for sname in self._raw_candidates():
                try:
                    url = SOURCES[sname]['raw_base'] + rel
                    r = self._get(url, timeout=30)
                    data = r.content
                    self.source_used = sname
                    break
                except Exception as e:
                    last_err = e
                    continue

            if data is None:
                raise RuntimeError(f'所有镜像源下载失败: {last_err}')
            json.loads(data)

            song_dir = os.path.join(self.tracks_dir, song_id)
            os.makedirs(song_dir, exist_ok=True)
            fname = f'Chart_{diff}.json' if diff != 'SP' else 'Chart.json'
            target_path = os.path.join(song_dir, fname)
            with open(target_path, 'wb') as f:
                f.write(data)
            say(f'已保存: {target_path} (源: {self.src["label"]})')
            if on_done:
                on_done(True, target_path, None)
        except Exception as e:
            if on_done:
                on_done(False, '', str(e))
            else:
                raise

    def download_async(self, song_id, diff, on_progress=None, on_done=None):
        t = threading.Thread(
            target=self.download_chart, args=(song_id, diff, on_progress, on_done), daemon=True)
        t.start()
        return t


__all__ = ['Downloader', 'SOURCES', 'DEFAULT_SOURCE']
