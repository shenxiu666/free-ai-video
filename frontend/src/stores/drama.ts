/**
 * Builder B · drama pinia store
 *
 * state 严格保持 { script, stateJson, loading } 三字段。
 * 缺后端时：newDrama/loadState 落 localStorage + fake 延迟，不抛阻断；
 * 只有“后端可达但明确拒绝”（如 400 校验失败）才向上抛。
 */
import { defineStore } from 'pinia';
import { createDrama, fetchDramaState, saveDramaScript, breakdownDrama } from '../api/drama';
import type { NewDramaPayload, BreakdownPayload } from '../api/drama';

export type VideoMode = 'text' | 'keyframe' | 'reference';
export type Aspect = '9:16' | '16:9';

export interface ScriptClip {
  id: string;
  start: number;
  duration: number;
  narration: string;
  image_prompt: string;
  video_prompt: string;
  video_mode: VideoMode;
  first_frame?: string;
  last_frame?: string;
  images?: string[];
  audios?: string[];
  /** 仅用于校验 visibility：reference 模式下出现即非法 */
  videos?: string[];
  srt_path?: string;
}

export interface DramaScript {
  title: string;
  total_seconds: number;
  aspect: Aspect;
  resolution: string;
  clip_seconds: string;
  character_refs: string[];
  clips: ScriptClip[];
  generated_by?: string;
}

export interface ValidationResult {
  ok: boolean;
  errors: string[];
  warnings: string[];
}

export interface RemoteResult {
  remote: boolean;
  message: string;
}

export interface NewDramaResult extends RemoteResult {
  script: DramaScript;
}

export const VIDEO_MODES: VideoMode[] = ['text', 'keyframe', 'reference'];
export const MIN_TOTAL_SECONDS = 60;
export const MAX_CHARACTER_REFS = 5;

const LS_SCRIPT_PREFIX = 'free-ai-video:script:';
const LS_STATE_PREFIX = 'free-ai-video:state:';
const FAKE_DELAY_MS = 600;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** 仅连接性错误才走本地 fallback；后端明确拒绝（4xx/5xx 报文）直接上抛 */
function isConnectivityError(e: unknown): boolean {
  const m = errMsg(e);
  return (
    m.includes('无法连接后端') ||
    m.includes('请求超时') ||
    m.includes('Failed to fetch') ||
    m.includes('NetworkError') ||
    m.includes('Load failed') ||
    m.includes('Network request failed') ||
    m.includes('不是合法 JSON')
  );
}

function lsGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function lsSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    /* 配额不足等：仅保留内存，不阻断 */
  }
}

function isNonEmptyString(v: unknown): v is string {
  return typeof v === 'string' && v.trim().length > 0;
}

function nonEmptyList(v: unknown): string[] {
  return Array.isArray(v) ? (v as unknown[]).filter(isNonEmptyString) : [];
}

function asRecord(v: unknown): Record<string, unknown> | null {
  return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : null;
}

function asString(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

/* ------------------------------------------------------------------ */
/* validate：01 + 04 校验规则                                           */
/* ------------------------------------------------------------------ */

export function validateScript(script: DramaScript | null | undefined): ValidationResult {
  const errors: string[] = [];
  const warnings: string[] = [];
  if (!script) {
    return { ok: false, errors: ['剧本为空：请先新建或加载'], warnings };
  }
  // 01：标题 / 时长下限 60s / 画幅
  if (!isNonEmptyString(script.title)) errors.push('剧名不能为空');
  if (!Number.isFinite(script.total_seconds) || script.total_seconds < MIN_TOTAL_SECONDS) {
    errors.push(`总时长下限 ${MIN_TOTAL_SECONDS}s，当前 ${String(script.total_seconds)}`);
  }
  if (script.aspect !== '9:16' && script.aspect !== '16:9') {
    errors.push(`画幅非法：${String(script.aspect)}（仅支持 9:16 / 16:9）`);
  }
  // 04：clip_seconds 必须是 "4"-"12" 字符串语义
  const per = Number(script.clip_seconds);
  if (!Number.isFinite(per) || !Number.isInteger(per) || per < 4 || per > 12) {
    errors.push(`单镜时长非法：${String(script.clip_seconds)}（应为 "4"-"12"）`);
  }
  // 04：character_refs ≤ 5
  if (!Array.isArray(script.character_refs)) {
    errors.push('角色锚点必须是数组');
  } else if (script.character_refs.length > MAX_CHARACTER_REFS) {
    errors.push(`角色锚点最多 ${MAX_CHARACTER_REFS} 张，当前 ${script.character_refs.length} 张`);
  }
  if (!Array.isArray(script.clips) || script.clips.length === 0) {
    errors.push('分镜不能为空：至少需要 1 镜');
    return { ok: false, errors, warnings };
  }
  // 逐镜校验
  const seen = new Set<string>();
  script.clips.forEach((clip, index) => {
    const label = `第${index + 1}镜(${(clip && clip.id) || '无编号'})`;
    if (!clip || typeof clip !== 'object') {
      errors.push(`${label}：分镜体不是对象`);
      return;
    }
    if (!isNonEmptyString(clip.id)) errors.push(`${label}：镜号不能为空`);
    else if (seen.has(clip.id)) errors.push(`镜号重复：${clip.id}`);
    else seen.add(clip.id);
    if (!Number.isFinite(clip.duration) || clip.duration <= 0) errors.push(`${label}：时长必须 > 0`);
    if (!Number.isFinite(clip.start) || clip.start < 0) errors.push(`${label}：开始秒非法`);
    if (!isNonEmptyString(clip.narration)) warnings.push(`${label}：台词为空，配音将无台词`);
    if (!isNonEmptyString(clip.image_prompt)) warnings.push(`${label}：画面提示词为空`);
    if (!isNonEmptyString(clip.video_prompt)) warnings.push(`${label}：运镜提示词为空`);
    // 04 §4.4：video_mode 三态媒体字段规则
    const mode = clip.video_mode;
    if (mode !== 'text' && mode !== 'keyframe' && mode !== 'reference') {
      errors.push(`${label}：生成模式非法（仅纯文本 / 首尾帧 / 参考图）`);
      return;
    }
    const hasFirst = isNonEmptyString(clip.first_frame);
    const hasLast = isNonEmptyString(clip.last_frame);
    const images = nonEmptyList(clip.images);
    const audios = nonEmptyList(clip.audios);
    const videos = nonEmptyList(clip.videos);
    if (mode === 'text') {
      if (hasFirst || hasLast || images.length > 0 || audios.length > 0 || videos.length > 0) {
        errors.push(`${label}：纯文本模式禁止携带首帧 / 尾帧 / 参考图 / 参考音频 / 视频`);
      }
    } else if (mode === 'keyframe') {
      if (!hasFirst && !hasLast) {
        errors.push(`${label}：首尾帧模式需首帧 / 尾帧至少其一`);
      }
      if (images.length > 0 || audios.length > 0) {
        warnings.push(`${label}：首尾帧模式下参考图 / 参考音频将被忽略`);
      }
    } else {
      if (images.length === 0 && audios.length === 0) {
        errors.push(`${label}：参考图模式需参考图(≤5) / 参考音频(≤3) 至少一类非空`);
      }
      if (images.length > 5) errors.push(`${label}：参考图最多 5 张，当前 ${images.length} 张`);
      if (audios.length > 3) errors.push(`${label}：参考音频最多 3 条，当前 ${audios.length} 条`);
      if (videos.length > 0) errors.push(`${label}：参考图模式禁止携带视频`);
      if (hasFirst || hasLast) {
        warnings.push(`${label}：参考图模式下首帧 / 尾帧将被忽略`);
      }
    }
  });
  // 04：sum(clips.duration) == total_seconds
  const sum = script.clips.reduce((acc, c) => acc + (Number(c && c.duration) || 0), 0);
  if (Math.abs(sum - script.total_seconds) > 1e-6) {
    errors.push(`时长不一致：分镜时长和=${sum}s ≠ 总时长=${script.total_seconds}s`);
  }
  // start 连续性（警告，不阻断，retime() 可一键修复）
  let cursor = 0;
  script.clips.forEach((clip, index) => {
    if (Math.abs((Number(clip.start) || 0) - cursor) > 1e-6) {
      warnings.push(`第${index + 1}镜开始秒=${String(clip.start)}，按累计应为 ${cursor}s，建议点“重排开始秒”`);
    }
    cursor += Number(clip.duration) || 0;
  });
  // 01：分辨率与画幅一致性（警告）
  const expectRes = script.aspect === '9:16' ? '720x1280' : script.aspect === '16:9' ? '1280x720' : '';
  if (expectRes && script.resolution && script.resolution !== expectRes) {
    warnings.push(`分辨率=${script.resolution} 与画幅=${script.aspect} 不符，建议 ${expectRes}`);
  }
  return { ok: errors.length === 0, errors, warnings };
}

/* ------------------------------------------------------------------ */
/* 默认剧本构造 / 远端归一化                                            */
/* ------------------------------------------------------------------ */

export function buildDefaultScript(payload: NewDramaPayload): DramaScript {
  const total = Math.max(MIN_TOTAL_SECONDS, Math.floor(payload.total_seconds));
  const per = Math.min(12, Math.max(4, Math.floor(Number(payload.clip_seconds) || 8)));
  const count = Math.ceil(total / per);
  const vertical = payload.aspect !== '16:9';
  const clips: ScriptClip[] = [];
  let cursor = 0;
  for (let i = 0; i < count; i++) {
    const id = `s${String(i + 1).padStart(2, '0')}`;
    const duration = Math.min(per, total - cursor);
    const prevId = `s${String(i).padStart(2, '0')}`;
    clips.push({
      id,
      start: cursor,
      duration,
      narration: `第${i + 1}镜旁白（待改写）`,
      image_prompt: `[主体]待补充+[场景]待补充+[风格]${payload.style}+[光照]待补充+[构图]${vertical ? '竖构图' : '横构图'}+[质量]1K,高细节`,
      video_prompt: `[主体]待补充+[动作]待补充+[场景]待补充+[运镜]缓慢推镜+[光照]待补充+[风格]${payload.style}`,
      // 首镜 text（无媒体字段即合法），后续 keyframe 链式衔接上一镜尾帧
      video_mode: i === 0 ? 'text' : 'keyframe',
      ...(i === 0 ? {} : { first_frame: `shots/${prevId}_last.png` }),
    });
    cursor += duration;
  }
  return {
    title: payload.title,
    total_seconds: total,
    aspect: payload.aspect,
    resolution: vertical ? '720x1280' : '1280x720',
    clip_seconds: String(per),
    character_refs: [],
    clips,
    generated_by: `frontend-local ${new Date().toISOString()}`,
  };
}

function normalizeClip(raw: unknown, index: number): ScriptClip {
  const r = asRecord(raw) ?? {};
  const modeRaw = r['video_mode'];
  const mode: VideoMode =
    modeRaw === 'keyframe' || modeRaw === 'reference' || modeRaw === 'text' ? modeRaw : 'text';
  return {
    id: isNonEmptyString(r['id']) ? (r['id'] as string) : `s${String(index + 1).padStart(2, '0')}`,
    start: Number(r['start']) || 0,
    duration: Number(r['duration']) || 0,
    narration: asString(r['narration']),
    image_prompt: asString(r['image_prompt']),
    video_prompt: asString(r['video_prompt']),
    video_mode: mode,
    ...(typeof r['first_frame'] === 'string' ? { first_frame: r['first_frame'] as string } : {}),
    ...(typeof r['last_frame'] === 'string' ? { last_frame: r['last_frame'] as string } : {}),
    ...(Array.isArray(r['images']) ? { images: nonEmptyList(r['images']) } : {}),
    ...(Array.isArray(r['audios']) ? { audios: nonEmptyList(r['audios']) } : {}),
    ...(Array.isArray(r['videos']) ? { videos: nonEmptyList(r['videos']) } : {}),
    ...(typeof r['srt_path'] === 'string' ? { srt_path: r['srt_path'] as string } : {}),
  };
}

function normalizeScript(raw: unknown, fallback: DramaScript): DramaScript {
  const r = asRecord(raw);
  if (!r) return fallback;
  const aspect: Aspect = r['aspect'] === '16:9' ? '16:9' : '9:16';
  const clipsRaw = Array.isArray(r['clips']) ? (r['clips'] as unknown[]) : [];
  return {
    title: isNonEmptyString(r['title']) ? (r['title'] as string) : fallback.title,
    total_seconds: Number(r['total_seconds']) || fallback.total_seconds,
    aspect,
    resolution: asString(r['resolution'], aspect === '9:16' ? '720x1280' : '1280x720'),
    clip_seconds: String(r['clip_seconds'] ?? fallback.clip_seconds),
    character_refs: Array.isArray(r['character_refs'])
      ? (r['character_refs'] as unknown[]).filter(isNonEmptyString)
      : [],
    clips: clipsRaw.length > 0 ? clipsRaw.map((c, i) => normalizeClip(c, i)) : fallback.clips,
    ...(typeof r['generated_by'] === 'string' ? { generated_by: r['generated_by'] as string } : {}),
  };
}

function looksLikeScript(v: unknown): boolean {
  const r = asRecord(v);
  return !!r && Array.isArray(r['clips']);
}

function persistLocal(name: string, script: DramaScript, state: Record<string, unknown> | null): void {
  lsSet(LS_SCRIPT_PREFIX + name, JSON.stringify(script));
  if (state) lsSet(LS_STATE_PREFIX + name, JSON.stringify(state));
}

function nextClipId(clips: ScriptClip[]): string {
  let max = 0;
  for (const c of clips) {
    const m = /^s(\d+)$/.exec(c.id || '');
    if (m) max = Math.max(max, Number(m[1]));
  }
  return `s${String(max + 1).padStart(2, '0')}`;
}

export interface LocalDraftInfo {
  name: string;
  clips: number;
  total_seconds: number;
}

/** 扫描本地草稿（localStorage），供新建页列表展示。 */
export function listLocalDrafts(): LocalDraftInfo[] {
  const out: LocalDraftInfo[] = [];
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (!k || !k.startsWith(LS_SCRIPT_PREFIX)) continue;
      const name = k.slice(LS_SCRIPT_PREFIX.length);
      try {
        const raw = localStorage.getItem(k);
        if (!raw) continue;
        const s = JSON.parse(raw) as Partial<DramaScript>;
        out.push({
          name,
          clips: Array.isArray(s.clips) ? s.clips.length : 0,
          total_seconds: Number(s.total_seconds) || 0,
        });
      } catch {
        out.push({ name, clips: 0, total_seconds: 0 });
      }
    }
  } catch {
    return [];
  }
  return out.sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'));
}

/** 删除本地草稿（含状态缓存）。 */
export function deleteLocalDraft(name: string): void {
  try {
    localStorage.removeItem(LS_SCRIPT_PREFIX + name);
    localStorage.removeItem(LS_STATE_PREFIX + name);
  } catch {
    /* 忽略 */
  }
}

/* ------------------------------------------------------------------ */
/* store                                                                */
/* ------------------------------------------------------------------ */

export const useDramaStore = defineStore('drama', {
  state: () => ({
    script: null as DramaScript | null,
    stateJson: null as Record<string, unknown> | null,
    loading: false as boolean,
  }),
  getters: {
    clipSum(state): number {
      return (state.script?.clips ?? []).reduce((acc, c) => acc + (Number(c.duration) || 0), 0);
    },
    durationConsistent(): boolean {
      if (!this.script) return false;
      return Math.abs(this.clipSum - this.script.total_seconds) < 1e-6;
    },
  },
  actions: {
    validate(): ValidationResult {
      return validateScript(this.script);
    },

    async newDrama(payload: NewDramaPayload): Promise<NewDramaResult> {
      this.loading = true;
      try {
        if (!payload.title.trim()) throw new Error('请填写剧名');
        if (!Number.isFinite(payload.total_seconds) || payload.total_seconds < MIN_TOTAL_SECONDS) {
          throw new Error(`总时长下限 ${MIN_TOTAL_SECONDS}s，当前 ${String(payload.total_seconds)}s`);
        }
        const per = Number(payload.clip_seconds);
        if (!Number.isFinite(per) || per < 4 || per > 12) {
          throw new Error('单镜时长需在 4-12s 之间');
        }
        const body: NewDramaPayload = {
          ...payload,
          title: payload.title.trim(),
          total_seconds: Math.floor(payload.total_seconds),
          clip_seconds: String(payload.clip_seconds),
        };
        try {
          const remote = await createDrama(body);
          const rec = asRecord(remote);
          const scriptRaw = rec && looksLikeScript(rec['script']) ? rec['script'] : remote;
          const script = normalizeScript(
            looksLikeScript(scriptRaw) ? scriptRaw : null,
            buildDefaultScript(body),
          );
          this.script = script;
          this.stateJson = rec && asRecord(rec['state']);
          persistLocal(script.title, script, this.stateJson);
          return { script, remote: true, message: '已创建并同步后端' };
        } catch (e) {
          // 缺后端 → 本地 fallback；后端明确拒绝 → 上抛
          if (!isConnectivityError(e)) throw e;
          await sleep(FAKE_DELAY_MS);
          const script = buildDefaultScript(body);
          this.script = script;
          this.stateJson = null;
          persistLocal(script.title, script, null);
          return { script, remote: false, message: `后端不可达，已存本地草稿：${errMsg(e)}` };
        }
      } finally {
        this.loading = false;
      }
    },

    /**
     * AI 分镜：原文 → script.json 初稿（后端不落盘，前端落本地草稿，去分镜表改完再保存）。
     * 后端明确拒绝（文本未配/原文为空/校验失败）直接上抛；连不上后端也上抛（无 AI 可调）。
     */
    async breakdown(payload: BreakdownPayload): Promise<NewDramaResult> {
      this.loading = true;
      try {
        if (!payload.name.trim()) throw new Error('请填写剧名');
        if (!(payload.source_text || '').trim()) throw new Error('请粘贴小说/剧本原文后再分解');
        const res = await breakdownDrama({ ...payload, name: payload.name.trim() });
        const fallback = buildDefaultScript({
          title: payload.name.trim(),
          total_seconds: payload.total_seconds,
          aspect: payload.aspect,
          clip_seconds: payload.clip_seconds,
          style: payload.style,
          brief: '',
        });
        const script = normalizeScript(looksLikeScript(res.script) ? res.script : null, fallback);
        this.script = script;
        this.stateJson = null;
        persistLocal(script.title, script, null);
        const hint = res.truncated ? '（原文超长已截断前 12000 字）' : '';
        return { script, remote: true, message: `AI 已分解出 ${script.clips.length} 镜${hint}，请到分镜表检查修改` };
      } finally {
        this.loading = false;
      }
    },
    async loadState(name: string): Promise<RemoteResult> {
      this.loading = true;
      try {
        try {
          const remote = await fetchDramaState(name);
          const remoteRec = asRecord(remote);
          const scriptRaw = remoteRec?.['script'];
          const fallback = buildDefaultScript({
            title: name,
            total_seconds: MIN_TOTAL_SECONDS,
            aspect: '9:16',
            clip_seconds: '8',
            style: '电影感写实',
            brief: '',
          });
          if (looksLikeScript(scriptRaw)) {
            this.script = normalizeScript(scriptRaw, fallback);
            this.stateJson = asRecord(remoteRec?.['state']);
            persistLocal(name, this.script as DramaScript, this.stateJson);
            return { remote: true, message: '已从后端载入' };
          }
          // 后端有状态但无剧本：先看本地草稿，绝不用空架子覆盖
          const raw = lsGet(LS_SCRIPT_PREFIX + name);
          if (raw) {
            this.script = JSON.parse(raw) as DramaScript;
            const st = lsGet(LS_STATE_PREFIX + name);
            this.stateJson = st ? (JSON.parse(st) as Record<string, unknown>) : asRecord(remoteRec?.['state']);
            await sleep(300);
            return { remote: false, message: '后端暂无剧本，已载入本地草稿，检查后点“保存全部”同步' };
          }
          this.script = null;
          this.stateJson = asRecord(remoteRec?.['state']);
          return { remote: true, message: '后端暂无剧本（只有空状态），请重新 AI 分解或检查剧名' };
        } catch (e) {
          const raw = lsGet(LS_SCRIPT_PREFIX + name);
          if (!raw) throw e instanceof Error ? e : new Error(`加载失败且本地无缓存：${String(e)}`);
          this.script = JSON.parse(raw) as DramaScript;
          const st = lsGet(LS_STATE_PREFIX + name);
          this.stateJson = st ? (JSON.parse(st) as Record<string, unknown>) : null;
          await sleep(300);
          // 404 = 后端暂无该剧（AI 分镜后尚未保存），与“连不上后端”区分提示
          if (errMsg(e).includes(' 404')) {
            return { remote: false, message: '已载入本地草稿（后端暂无该剧，检查修改后点“保存全部”同步）' };
          }
          return { remote: false, message: `后端不可达，已载入本地缓存：${errMsg(e)}` };
        }
      } finally {
        this.loading = false;
      }
    },

    /** 更新一镜：先 PUT 本地 store + 落盘，再 POST 后端；远端失败保留本地并回提示 */
    async saveClip(clipId: string, patch: Partial<ScriptClip>): Promise<RemoteResult> {
      if (!this.script) return { remote: false, message: '剧本为空，无法保存' };
      const clip = this.script.clips.find((c) => c.id === clipId);
      if (!clip) return { remote: false, message: `未找到分镜 ${clipId}` };
      Object.assign(clip, patch);
      persistLocal(this.script.title, this.script, this.stateJson);
      try {
        await saveDramaScript(this.script.title, this.script);
        return { remote: true, message: `${clipId} 已保存（本地 + 后端）` };
      } catch (e) {
        return { remote: false, message: `${clipId} 已存本地，后端同步失败：${errMsg(e)}（稍后点“保存全部”重试）` };
      }
    },

    async saveAll(): Promise<RemoteResult> {
      if (!this.script) return { remote: false, message: '剧本为空，无法保存' };
      persistLocal(this.script.title, this.script, this.stateJson);
      try {
        await saveDramaScript(this.script.title, this.script);
        return { remote: true, message: '已保存（本地 + 后端）' };
      } catch (e) {
        return { remote: false, message: `已存本地，后端同步失败：${errMsg(e)}（联网后点“保存全部”重试）` };
      }
    },

    addClip(): ScriptClip | null {
      if (!this.script) return null;
      const per = Math.min(12, Math.max(4, Math.floor(Number(this.script.clip_seconds) || 8)));
      const clips = this.script.clips;
      const prev = clips[clips.length - 1];
      const clip: ScriptClip = {
        id: nextClipId(clips),
        start: clips.reduce((acc, c) => acc + (Number(c.duration) || 0), 0),
        duration: per,
        narration: '',
        image_prompt: '',
        video_prompt: '',
        video_mode: prev ? 'keyframe' : 'text',
        ...(prev
          ? {
              first_frame: isNonEmptyString(prev.last_frame)
                ? (prev.last_frame as string)
                : `shots/${prev.id}_last.png`,
            }
          : {}),
      };
      clips.push(clip);
      persistLocal(this.script.title, this.script, this.stateJson);
      return clip;
    },

    removeClip(clipId: string): void {
      if (!this.script) return;
      this.script.clips = this.script.clips.filter((c) => c.id !== clipId);
      persistLocal(this.script.title, this.script, this.stateJson);
    },

    /** 按 duration 顺序重排 start（修复 start 连续性警告） */
    retime(): void {
      if (!this.script) return;
      let cursor = 0;
      for (const c of this.script.clips) {
        c.start = cursor;
        cursor += Number(c.duration) || 0;
      }
      persistLocal(this.script.title, this.script, this.stateJson);
    },

    setCharacterRefs(refs: string[]): void {
      if (!this.script) return;
      this.script.character_refs = refs;
      persistLocal(this.script.title, this.script, this.stateJson);
    },
  },
});
