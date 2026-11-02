"""mvfx —— MV 剪辑效果工具箱：不用 AI 视频，用代码做闪切、交替、频闪、白闪、慢推、字卡、按镜头表剪辑等。

依赖：Python 3.9+、numpy、opencv-python、Pillow；ffmpeg 要能在命令行直接运行。
查看全部子命令：python mvfx.py -h        查看某个子命令：python mvfx.py montage -h

通用参数：
  --size 1280x720   输出画面大小（处理单个视频的命令，不填就沿用原视频大小）
  --fps             输出帧率（处理单个视频的命令默认跟原视频一样，其余默认 24）
  --bpm             每分钟多少拍（时间写成 4b、@2.1 时需要）
  时间写法（所有时间参数都认）：
    1.5    = 第 1.5 秒
    4b     = 从这段素材开头数过 4 拍的位置（4b 就是拍位 2.1）
    @2.1   = 分镜表里的拍位：第 2 小节第 1 拍；@2.3+ = 第 2 小节第 3 拍的后半拍（按每小节 4 拍算）
    6f     = 第 6 帧
  数值是负数时用等号写，比如 --pan-from=-0.05,0
"""
import argparse, csv, glob, json, math, os, re, subprocess, sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

IMG_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.bmp')
COLORS = {  # BGR
    'white': (255, 255, 255), 'black': (0, 0, 0), 'red': (30, 20, 220), 'blue': (220, 90, 20),
    'klein': (167, 47, 0), 'yellow': (40, 230, 250), 'lemon': (60, 235, 215), 'green': (60, 200, 60),
    'mint': (190, 230, 170), 'orange': (20, 120, 245), 'pink': (180, 100, 240), 'purple': (200, 60, 140),
    'teal': (150, 140, 30), 'gray': (128, 128, 128),
}
FONT_CANDIDATES = [
    'C:/Windows/Fonts/msyhbd.ttc', 'C:/Windows/Fonts/msyh.ttc', 'C:/Windows/Fonts/arialbd.ttf',
    '/System/Library/Fonts/PingFang.ttc', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
]
HANGUL_FONTS = [  # 微软雅黑没有韩文字，有韩文时换这些
    'C:/Windows/Fonts/malgunbd.ttf', 'C:/Windows/Fonts/malgun.ttf',
    '/System/Library/Fonts/AppleSDGothicNeo.ttc', '/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf',
]


# ---------------------------------------------------------------- 基础工具
def die(msg):
    sys.exit('出错了：' + msg)


def parse_size(s):
    try:
        w, h = s.lower().split('x')
        w, h = int(w) // 2 * 2, int(h) // 2 * 2
        return w, h
    except Exception:
        die(f'画面大小写法不对：{s}（应写成 1280x720）')


def parse_color(s):
    s = s.strip().lower()
    if s in COLORS:
        return COLORS[s]
    if s.startswith('#') and len(s) == 7:
        r, g, b = int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16)
        return (b, g, r)
    die(f'不认识的颜色：{s}（可用 {", ".join(COLORS)} 或 #RRGGBB）')


def parse_time(s, bpm=None, fps=24):
    s = str(s).strip().lower()
    try:
        if s.startswith('@'):
            if not bpm:
                die(f'"{s}" 是按拍位写的时间，需要同时给 --bpm')
            m = re.match(r'^@(\d+)\.(\d+)(\+?)$', s)
            if not m:
                die(f'拍位 "{s}" 写法不对，应写成 @2.1 或 @2.3+')
            bar, beat, half = int(m.group(1)), int(m.group(2)), m.group(3)
            return ((bar - 1) * 4 + (beat - 1) + (0.5 if half else 0)) * 60.0 / bpm
        if s.endswith('b') or s.endswith('拍'):
            if not bpm:
                die(f'"{s}" 是按拍写的时间，需要同时给 --bpm')
            return float(s.rstrip('b拍')) * 60.0 / bpm
        if s.endswith('f') or s.endswith('帧'):
            return float(s.rstrip('f帧')) / fps
        return float(s.rstrip('s秒'))
    except ValueError:
        die(f'时间 "{s}" 看不懂，应写成 1.5（秒）、4b（第几拍）、@2.1（拍位）或 6f（第几帧）')


def tval(s):
    """时间参数先原样收下，等知道 --bpm 和帧率以后再换算成秒。"""
    try:
        return float(s)
    except ValueError:
        return str(s)


TIME_ATTRS = ['from_', 'dur', 'start', 'end', 'length', 'a_from', 'b_from', 'dur_a', 'dur_b', 'audio_start',
              'label_start', 'beat0', 'lead']


def parse_times(s, bpm=None, fps=24):
    return [parse_time(x, bpm, fps) for x in str(s).split(',') if x.strip()]


def imread(path):
    """读图片，支持中文路径。"""
    if not os.path.exists(path):
        die(f'找不到文件：{path}')
    try:
        im = ImageOps.exif_transpose(Image.open(path))   # 手机照片按 EXIF 方向转正
    except Exception:
        die(f'读不了图片：{path}')
    if im.mode.startswith('I'):                           # 16 位灰度
        im = Image.fromarray((np.asarray(im, dtype=np.uint32) // 257).astype(np.uint8))
    has_alpha = 'A' in im.getbands() or 'transparency' in im.info
    a = np.asarray(im.convert('RGBA' if has_alpha else 'RGB'))
    return cv2.cvtColor(a, cv2.COLOR_RGBA2BGRA if has_alpha else cv2.COLOR_RGB2BGR)


def imwrite(path, img):
    ok, buf = cv2.imencode(os.path.splitext(path)[1] or '.jpg', img)
    if not ok:
        die(f'存不了图片：{path}')
    buf.tofile(path)


def cover(img, W, H):
    """等比缩放后居中裁切，铺满 W×H。"""
    h, w = img.shape[:2]
    s = max(W / w, H / h)
    nw, nh = max(W, round(w * s)), max(H, round(h * s))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    x, y = (nw - W) // 2, (nh - H) // 2
    return r[y:y + H, x:x + W]


def ease(u, kind='inout'):
    u = min(max(u, 0.0), 1.0)
    if kind == 'in':
        return u * u
    if kind == 'out':
        return 1 - (1 - u) * (1 - u)
    if kind == 'linear':
        return u
    return 0.5 - 0.5 * math.cos(math.pi * u)


def zoom(frame, z, cx=0.5, cy=0.5):
    """以 (cx,cy)（画面比例）为中心放大 z 倍。"""
    H, W = frame.shape[:2]
    px, py = cx * W, cy * H
    M = np.float32([[z, 0, px - z * px], [0, z, py - z * py]])
    return cv2.warpAffine(frame, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


class Clip:
    """视频或图片素材。frame(t) 取第 t 秒的画面；图片永远返回同一张。"""

    def __init__(self, path):
        if not os.path.exists(path):
            die(f'找不到文件：{path}')
        self.path = path
        self.still = path.lower().endswith(IMG_EXT)
        if self.still:
            img = imread(path)
            if img.shape[2] == 4:  # 透明的地方当成黑色，不露出藏在透明里的杂色
                al = img[:, :, 3:4].astype(np.float32) / 255.0
                img = (img[:, :, :3].astype(np.float32) * al).astype(np.uint8)
            self.img = img
            self.overrun = 0
            self.fps, self.n, self.dur = None, None, None
            self.size = (self.img.shape[1] // 2 * 2, self.img.shape[0] // 2 * 2)
        else:
            self.cap = cv2.VideoCapture(path)
            if not self.cap.isOpened():
                die(f'打不开视频：{path}')
            self.fps = self.cap.get(cv2.CAP_PROP_FPS) or 24.0
            self.n = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self.claimed, self.incomplete, self.badread = self.n, False, 0
            written, real = probe_frames(path)  # 文件里写的帧数不一定都读得出来（没下载完 / 没拷完）
            if real:
                self.n = min(self.n, real) if self.n > 0 else real
                # 只有文件里明确写了帧数（mp4 / mov）且比实际多，才算不完整；mkv / webm 没写帧数，不误报
                if written and written > real:
                    self.claimed, self.incomplete = written, True
                    print(f'注意：{os.path.basename(path)} 可能不完整：写着 {written} 帧，只读得出 {real} 帧'
                          f'（前 {real / self.fps:.2f} 秒）')
            self.dur = self.n / self.fps
            self.size = (int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH)) // 2 * 2,
                         int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) // 2 * 2)
            self.pos, self.last, self.overrun = -1, None, 0

    def frame(self, t):
        if self.still:
            return self.img
        want = int(round(t * self.fps))
        if want > self.n - 1:
            self.overrun += 1
        i = min(max(0, want), max(self.n - 1, 0))
        if i == self.pos and self.last is not None:
            return self.last
        if i != self.pos + 1:
            if 0 < i - self.pos <= 8:          # 往后跳几帧：顺着读比重新定位快得多
                for _ in range(i - self.pos - 1):
                    self.cap.grab()
            else:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, f = self.cap.read()
        if not ok:
            if self.last is None:
                die(f'读不到画面：{self.path} 第 {t:.2f} 秒')
            self.badread += 1  # 读失败拿前一帧顶上，报告里单独写出来
            f = self.last
            self.pos, self.last = -10, f  # 下一帧重新定位，不接着顺序读，避免后面整体错一帧
            return f
        self.pos, self.last = i, f
        return f


def probe_frames(path):
    """用 ffprobe 查：文件里写着几帧（mkv / webm 没有这个值），实际有几帧（只数数据包，不解码，很快）。"""
    try:
        r = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-count_packets',
                            '-show_entries', 'stream=nb_frames,nb_read_packets', '-of', 'json', path],
                           capture_output=True, text=True, timeout=60)
        s = json.loads(r.stdout)['streams'][0]

        def num(v):
            return int(v) if str(v).isdigit() else None
        return num(s.get('nb_frames')), num(s.get('nb_read_packets'))
    except Exception:
        return None, None


class Writer:
    """把画面一帧帧交给 ffmpeg 编码成 mp4；可选配上音乐。"""

    def __init__(self, path, W, H, fps, audio=None, audio_start=0.0):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        if audio:
            if not os.path.exists(audio):
                die(f'找不到音乐文件：{audio}')
            d = media_dur(audio)
            if d is not None and audio_start >= d:
                die(f'--audio-start {audio_start} 秒超过了音乐总长 {d:.1f} 秒')
        cmd = ['ffmpeg', '-y', '-v', 'error', '-f', 'rawvideo', '-pix_fmt', 'bgr24',
               '-s', f'{W}x{H}', '-r', str(fps), '-i', '-']
        if audio:
            cmd += ['-ss', str(audio_start), '-i', audio, '-map', '0:v', '-map', '1:a?',
                    '-af', 'apad', '-c:a', 'aac', '-shortest']   # 音乐不够长就补静音，画面不会被截短
        cmd += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-crf', '18', '-preset', 'medium',
                '-movflags', '+faststart', path]
        try:
            self.p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        except FileNotFoundError:
            die('找不到 ffmpeg，请先安装并确认命令行里能运行 ffmpeg')
        self.W, self.H, self.n, self.path, self.fps = W, H, 0, path, fps

    def write(self, f):
        if f.shape[1] != self.W or f.shape[0] != self.H:
            f = cover(f, self.W, self.H)
        try:
            self.p.stdin.write(np.ascontiguousarray(f, dtype=np.uint8).tobytes())
        except OSError:
            self.p.wait()
            die('ffmpeg 中途停了（上面几行英文是 ffmpeg 给的原因）。常见原因：音乐文件打不开、输出文件名没写 .mp4')
        self.n += 1

    def close(self):
        self.p.stdin.close()
        self.p.wait()
        if self.p.returncode:
            die('ffmpeg 编码失败')
        print(f'完成：{self.path}（{self.n} 帧，{self.n / self.fps:.2f} 秒）')


def media_dur(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'default=nw=1:nk=1', path], capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def check_out(out, *inputs):
    for i in inputs:
        if i and os.path.abspath(out).lower() == os.path.abspath(i).lower():
            die(f'输出文件和输入文件是同一个（{out}），会把原素材毁掉，请换一个 -o 名字')


def warn_late(starts, nf, fps):
    late = [s for s in starts if s >= nf]
    if late:
        print(f'注意：有 {len(late)} 个时间点（' + '、'.join(f'{s / fps:.2f}秒' for s in late)
              + f'）超过了视频长度 {nf / fps:.2f} 秒，不会生效')


def warn_frozen(*clips):
    for c in clips:
        if c is not None and not c.still and c.overrun:
            print(f'注意：{os.path.basename(c.path)} 只有 {c.dur:.2f} 秒，不够用，最后 {c.overrun} 帧是定格')
        if c is not None and not c.still and c.badread:
            print(f'注意：{os.path.basename(c.path)} 有 {c.badread} 帧读坏了，用前一帧代替')


def out_size(args, clip=None):
    if getattr(args, 'size', None):
        return parse_size(args.size)
    if clip is not None:
        return clip.size
    return (1280, 720)


def clip_frames(clip, args):
    """一个素材要处理多少帧：视频默认从 --from 到结尾，图片必须给 --dur。"""
    start = getattr(args, 'from_', 0.0) or 0.0
    if getattr(args, 'dur', None):
        dur = args.dur
    elif clip.still:
        die('输入是图片，需要用 --dur 说明要做几秒')
    else:
        dur = clip.dur - start
    return start, int(round(dur * args.fps))


def run_per_frame(args, fn):
    """通用：逐帧处理一个素材。fn(frame, k, t) 返回新画面。"""
    clip = getattr(args, '_clip', None) or Clip(args.input)
    check_out(args.out, args.input)
    if not args.fps:
        args.fps = round(clip.fps, 3) if not clip.still else 24
    W, H = out_size(args, clip)
    start, nf = clip_frames(clip, args)
    if nf <= 0:
        die(f'--from {start} 秒已经超过了素材长度 {clip.dur:.2f} 秒')
    if getattr(args, 'at', None):
        warn_late([int(round(t * args.fps)) for t in parse_times(args.at, getattr(args, 'bpm', None), args.fps)], nf, args.fps)
    w = Writer(args.out, W, H, args.fps, None if clip.still else args.input, start)
    for k in range(nf):
        t = k / args.fps
        f = cover(clip.frame(start + t), W, H)
        w.write(fn(f, k, t))
    w.close()
    if clip.overrun:
        print(f'注意：素材只有 {clip.dur:.2f} 秒，最后 {clip.overrun} 帧是定格的最后一帧')
    if not clip.still and clip.badread:
        print(f'注意：素材有 {clip.badread} 帧读坏了，用前一帧代替')


def in_windows(k, starts, length):
    """第 k 帧是否落在某个效果窗口里；返回窗口内第几帧（-1 表示不在）。"""
    for s in starts:
        if s <= k < s + length:
            return k - s
    return -1


# ---------------------------------------------------------------- 子命令
def cmd_beats(a):
    spb = 60.0 / a.bpm
    total = a.bars * a.per_bar
    print(f'BPM {a.bpm}：一拍 {spb:.3f} 秒，半拍 {spb / 2:.3f} 秒，一小节 {spb * a.per_bar:.3f} 秒；'
          f'{a.bars} 小节 = {total} 拍 = {total * spb:.2f} 秒')
    rows = []
    for i in range(total * 2):
        beat = i / 2
        t = a.start + beat * spb
        bar, b = int(beat // a.per_bar) + 1, beat % a.per_bar + 1
        label = f'{bar}.{int(b)}' + ('+' if i % 2 else '')
        rows.append((label, round(t, 3), int(round(t * a.fps))))
    print('拍位\t秒\t帧号')
    for r in rows:
        print(f'{r[0]}\t{r[1]}\t{r[2]}')
    if a.csv:
        with open(a.csv, 'w', newline='', encoding='utf-8-sig') as f:
            csv.writer(f).writerows([('拍位', '秒', '帧号')] + rows)
        print('已存：' + a.csv)


def cmd_montage(a):
    files = list(a.files or [])
    if a.dir:
        if not os.path.isdir(a.dir):
            die(f'找不到文件夹：{a.dir}')
        nat = lambda n: [int(x) if x.isdigit() else x.lower() for x in re.split(r'(\d+)', n)]
        files += [os.path.join(a.dir, n) for n in sorted(os.listdir(a.dir), key=nat)
                  if n.lower().endswith(IMG_EXT + ('.mp4', '.mov', '.mkv', '.webm'))]
    if not files:
        die('没有素材：用 --dir 给文件夹，或在后面直接列文件')
    check_out(a.out, *files, a.audio)
    files = files * a.loop
    n = len(files)
    # 每个素材占几帧
    if a.beats:
        spb = 60.0 / a.bpm if a.bpm else die('--beats 需要同时给 --bpm')
        bounds = [int(round(i * a.beats * spb * a.fps)) for i in range(n + 1)]
        lens = [bounds[i + 1] - bounds[i] for i in range(n)]
    elif a.accel:
        try:
            f0, f1 = [float(x) for x in a.accel.replace('：', ':').split(':')]
        except ValueError:
            die(f'--accel 要写成 起始帧数:结束帧数，比如 9:5（你写的是 {a.accel}）')
        lens = [max(1, int(round(f0 + (f1 - f0) * (i / max(n - 1, 1))))) for i in range(n)]
    else:
        lens = [a.frames] * n
    opened = {}
    for f_ in files:  # 同一个文件只打开一次
        if f_ not in opened:
            opened[f_] = Clip(f_)
    clips = [opened[f_] for f_ in files]
    W, H = out_size(a)
    w = Writer(a.out, W, H, a.fps, a.audio, a.audio_start)
    if a.ghost:
        ghost_montage(a, clips, lens, W, H, w)
    else:
        for c, L in zip(clips, lens):
            for j in range(L):
                f = cover(c.frame(j / a.fps), W, H)
                if a.drift:
                    f = zoom(f, 1 + a.drift * (j / max(L - 1, 1)))
                w.write(f)
    w.close()
    print('每个素材的帧数：' + ','.join(map(str, lens)))
    warn_frozen(*opened.values())


def lighten(base, layer, alpha):
    """只叠比底图亮的地方（screen 叠加会把暗部抬灰，切点那帧像起雾）。"""
    return np.maximum(base, layer * alpha)


def rgb_split(f, px):
    out = f.copy()
    out[..., 2] = np.roll(f[..., 2], px, axis=1)    # 红往右
    out[..., 0] = np.roll(f[..., 0], -px, axis=1)   # 蓝往左
    return out


def ghost_montage(a, clips, lens, W, H, w):
    """点名闪切加剪辑运动：每张一切进来往指定位置快推（先快后慢，头 4 帧带运动模糊）；
    新的一张带一层大 12% 的虚影 3 帧收回；上一张继续放大、变淡留 3 帧残影；每切第一帧红蓝错位、第二帧减半。"""
    n = len(clips)
    centers = [(0.5, 0.5)] * n
    if a.centers:
        pts = [p for p in re.split(r'[;\s]+', a.centers.strip()) if p]
        try:
            pts = [tuple(float(x) for x in p.split(',')) for p in pts]
            if any(len(q) != 2 for q in pts):
                raise ValueError
        except ValueError:
            die(f'--centers 要写成 x,y;x,y（画面比例），比如 0.46,0.42;0.3,0.45（你写的是 {a.centers}）')
        centers = [pts[i % len(pts)] for i in range(n)]
    pushes = [a.push] * n
    if a.push_last is not None:
        pushes[-1] = a.push_last
    px = max(1, int(round(8 * W / 1152)))
    out3 = lambda u: 1 - (1 - min(max(u, 0.0), 1.0)) ** 3
    prev = None   # 上一张最后一帧、它最后的放大倍数、中心
    for i, (c, L) in enumerate(zip(clips, lens)):
        cx, cy = centers[i]
        sc = lambda u, i=i, L=L: 1 + pushes[i] * out3(u / max(L, 1))
        for j in range(L):
            base = cover(c.frame(j / a.fps), W, H).astype(np.float32)
            subs = [j + d for d in (-0.375, -0.125, 0.125, 0.375) if j + d >= 0] if j < 4 else [j]
            f = np.mean([zoom(base, sc(u), cx, cy) for u in subs], axis=0)
            if j < 3:
                f = lighten(f, zoom(base, sc(j) * (1 + 0.12 * (1 - j / 3)), cx, cy), 0.6 * (1 - j / 3))
                if prev is not None:
                    pf, ps, (pcx, pcy) = prev
                    f = lighten(f, zoom(pf, ps * (1 + 0.05 * (j + 1)), pcx, pcy), (0.75, 0.45, 0.2)[j])
            if j < 2:
                f = rgb_split(f, (px, max(1, px // 2))[j])
            w.write(np.clip(f, 0, 255).astype(np.uint8))
        prev = (cover(c.frame((L - 1) / a.fps), W, H).astype(np.float32), sc(L), centers[i])


def cmd_kenburns(a):
    check_out(a.out, a.image, a.fg)
    img = imread(a.image)
    fg = None
    if img.shape[2] == 4:  # 透明的地方当黑色，不露出藏在透明里的颜色
        al = img[:, :, 3:4].astype(np.float32) / 255.0
        img = (img[:, :, :3].astype(np.float32) * al).astype(np.uint8)
    W, H = out_size(a)
    if a.fg:
        fg = imread(a.fg)
        if fg.shape[2] != 4:
            die('--fg 前景图要带透明通道的 PNG（抠好的人或物）')
        if fg.shape[:2] != img.shape[:2]:
            die(f'--fg 前景 PNG 要和背景图一样大（现在背景 {img.shape[1]}x{img.shape[0]}，前景 {fg.shape[1]}x{fg.shape[0]}）：整幅画面大小、人以外的地方透明')
    if min(a.zoom_from, a.zoom_to) < 1.0:
        die('--zoom-from / --zoom-to 不能小于 1（小于 1 会露出镜像的边）。慢拉请写 --zoom-from 1.12 --zoom-to 1.0')
    nf = int(round(a.dur * a.fps))
    def pan(s, name):
        try:
            x, y = [float(v) for v in s.replace('，', ',').split(',')]
            return [x, y]
        except ValueError:
            die(f'{name} 要写成"左右,上下"两个数，比如 0.05,0（你写的是 {s}）')
    p0, p1 = pan(a.pan_from, '--pan-from'), pan(a.pan_to, '--pan-to')

    clamped = []

    def render(src, z, cx, cy, border=cv2.BORDER_REFLECT):
        h, w = src.shape[:2]
        s = max(W / w, H / h) * z
        mx, my = max(0.0, 0.5 - W / (2 * s * w)), max(0.0, 0.5 - H / (2 * s * h))
        if border == cv2.BORDER_REFLECT and (abs(cx) > mx + 1e-6 or abs(cy) > my + 1e-6):
            clamped.append(1)
            cx, cy = min(max(cx, -mx), mx), min(max(cy, -my), my)   # 平移不超出原图，不露镜像边
        sx, sy = w / 2 + cx * w, h / 2 + cy * h
        M = np.float32([[s, 0, W / 2 - s * sx], [0, s, H / 2 - s * sy]])
        return cv2.warpAffine(src, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=border, borderValue=0)

    # 先把底图缩到够用的大小，避免每帧处理超大图
    lim = 2.0 * max(W, H) * max(a.zoom_from, a.zoom_to)
    if max(img.shape[:2]) > lim:
        r = lim / max(img.shape[:2])
        img = cv2.resize(img, None, fx=r, fy=r, interpolation=cv2.INTER_AREA)
    w = Writer(a.out, W, H, a.fps)
    for k in range(nf):
        u = ease(k / max(nf - 1, 1), a.ease)
        z = a.zoom_from + (a.zoom_to - a.zoom_from) * u
        cx, cy = p0[0] + (p1[0] - p0[0]) * u, p0[1] + (p1[1] - p0[1]) * u
        f = render(img, z, cx, cy)
        if fg is not None:  # 两层视差：前景比背景动得多
            zf = 1 + (z - 1) * a.fg_mult
            layer = render(fg, zf, cx * a.fg_mult, cy * a.fg_mult, cv2.BORDER_CONSTANT)
            alpha = layer[:, :, 3:4].astype(np.float32) / 255.0
            f = (layer[:, :, :3] * alpha + f * (1 - alpha)).astype(np.uint8)
        w.write(f)
    w.close()
    if clamped:
        print('注意：平移幅度超过了图片能移动的范围，已自动收住（想移得多，就把 --zoom-to 调大）')


def cmd_interleave(a):
    check_out(a.out, a.a, a.b)
    A = Clip(a.a)
    B = A if os.path.abspath(a.b) == os.path.abspath(a.a) else Clip(a.b)
    W, H = out_size(a, A)
    nf = int(round(a.dur * a.fps))
    w = Writer(a.out, W, H, a.fps)
    for k in range(nf):
        t = k / a.fps
        fa = A.frame(a.a_from + t)  # 两条都按时间连续往下读，动作才接得上
        fb = B.frame(a.b_from + t)
        w.write(cover(fa if (k // a.every) % 2 == 0 else fb, W, H))
    w.close()
    warn_frozen(*({id(c): c for c in (A, B)}.values()))


def cmd_strobe(a):
    def fn(f, k, t):
        s, e = a.start * a.fps, (a.end * a.fps if a.end else 1e18)
        if s <= k < e and (k - s) % (a.on + a.off) >= a.on:
            return (f.astype(np.float32) * a.dark).astype(np.uint8)
        return f
    run_per_frame(a, fn)


def cmd_color(a):
    cols = [np.array(parse_color(c), np.float32) for c in a.colors.split(',')]
    def fn(f, k, t):
        s, e = a.start * a.fps, (a.end * a.fps if a.end else 1e18)
        if not (s <= k < e):
            return f
        c = cols[int((k - s) // a.every) % len(cols)]
        return (f.astype(np.float32) * (1 - a.strength) + c * a.strength).astype(np.uint8)
    run_per_frame(a, fn)


def cmd_invert(a):
    st = lambda: [int(round(t * a.fps)) for t in parse_times(a.at, a.bpm, a.fps)]
    run_per_frame(a, lambda f, k, t: 255 - f if in_windows(k, st(), a.frames) >= 0 else f)


def cmd_flash(a):
    st = lambda: [int(round(t * a.fps)) for t in parse_times(a.at, a.bpm, a.fps)]
    col = np.array(parse_color(a.color), np.float32)

    def fn(f, k, t):
        j = in_windows(k, st(), a.frames)
        if j < 0:
            return f
        alpha = 1.0 if j < a.hold else 1.0 - (j - a.hold + 1) / (a.frames - a.hold + 1)
        return (f.astype(np.float32) * (1 - alpha) + col * alpha).astype(np.uint8)
    run_per_frame(a, fn)


def glitch_frame(f, k, amount=0.02, seed=0):
    rng = np.random.default_rng(seed * 1000 + k)
    H, W = f.shape[:2]
    dx = max(2, int(W * amount))
    g = f.copy()
    g[:, :, 2] = np.roll(f[:, :, 2], dx, axis=1)    # 红色往右
    g[:, :, 0] = np.roll(f[:, :, 0], -dx, axis=1)   # 蓝色往左
    for _ in range(rng.integers(3, 8)):              # 横向错位条
        y = int(rng.integers(0, H - 10))
        hh = int(rng.integers(4, max(5, H // 12)))
        g[y:y + hh] = np.roll(g[y:y + hh], int(rng.integers(-W // 8, W // 8)), axis=1)
    return g


def cmd_glitch(a):
    st = lambda: [int(round(t * a.fps)) for t in parse_times(a.at, a.bpm, a.fps)]
    run_per_frame(a, lambda f, k, t: glitch_frame(f, k, a.amount, a.seed) if in_windows(k, st(), a.frames) >= 0 else f)


def cmd_punch(a):
    def fn(f, k, t):
        starts = sorted(int(round(x * a.fps)) for x in parse_times(a.at, a.bpm, a.fps))
        prev = [s for s in starts if s <= k]
        if not prev:
            return f
        j = k - prev[-1]
        z_from = 1.0 if (a.reset or len(prev) == 1) else 1.0 + (a.scale - 1.0) * (len(prev) - 1)
        z_to = z_from + (a.scale - 1.0)
        if a.reset:
            z_from, z_to = 1.0, a.scale
        if j < a.ramp:  # 推的过程加一点动态模糊（多张不同放大倍数叠在一起）
            u0, u1 = ease(j / a.ramp, 'out'), ease((j + 1) / a.ramp, 'out')
            subs = [zoom(f, z_from + (z_to - z_from) * (u0 + (u1 - u0) * i / 4)) for i in range(5)]
            return np.mean(subs, axis=0).astype(np.uint8)
        return zoom(f, z_to)
    run_per_frame(a, fn)


def motion_blur(f, length, horizontal=True):
    L = max(1, int(length))
    if L <= 1:
        return f
    return cv2.blur(f, (L, 1) if horizontal else (1, L))


def cmd_whip(a):
    check_out(a.out, a.a, a.b)
    A = Clip(a.a)
    B = A if os.path.abspath(a.b) == os.path.abspath(a.a) else Clip(a.b)
    W, H = out_size(a, A)
    horiz = a.dir in ('left', 'right')
    sign = 1 if a.dir in ('right', 'down') else -1  # 镜头往右甩 → 画面内容往左跑
    span = W if horiz else H
    na, nb, N = int(round(a.dur_a * a.fps)), int(round(a.dur_b * a.fps)), a.frames
    w = Writer(a.out, W, H, a.fps)

    def shift(f, d):
        M = np.float32([[1, 0, d], [0, 1, 0]]) if horiz else np.float32([[1, 0, 0], [0, 1, d]])
        return cv2.warpAffine(f, M, (W, H), borderMode=cv2.BORDER_REFLECT)
    for k in range(na):  # A：最后 N 帧加速甩出
        f = cover(A.frame(a.a_from + k / a.fps), W, H)
        j = k - (na - N)
        if j >= 0:
            u = ease((j + 1) / N, 'in')
            f = motion_blur(shift(f, -sign * u * span * 0.35), u * span * 0.25, horiz)
        w.write(f)
    for k in range(nb):  # B：开头 N 帧从模糊里甩进来、停稳
        f = cover(B.frame(a.b_from + k / a.fps), W, H)
        if k < N:
            u = 1 - ease((k + 1) / N, 'out')
            f = motion_blur(shift(f, sign * u * span * 0.35), u * span * 0.25, horiz)
        w.write(f)
    w.close()
    warn_frozen(*({id(c): c for c in (A, B)}.values()))


def cmd_spin(a):
    """滚转：在一段时间内把画面转过 --deg 度，自动放大盖住四角的黑边。"""
    def fn(f, k, t):
        s0 = a.start * a.fps
        n = max(1, int(round(a.length * a.fps)))
        u = ease((k - s0) / n, a.ease) if k >= s0 else 0.0
        ang = a.from_deg + (a.deg - a.from_deg) * u
        th = math.radians(ang)
        H, W = f.shape[:2]
        c, s = abs(math.cos(th)), abs(math.sin(th))
        cover_scale = max((W * c + H * s) / W, (W * s + H * c) / H)
        M = cv2.getRotationMatrix2D((W / 2, H / 2), ang, cover_scale)
        return cv2.warpAffine(f, M, (W, H), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    run_per_frame(a, fn)


def cmd_look(a):
    state = {}

    def fn(f, k, t):
        h, w_ = f.shape[:2]
        if 'vig' not in state:
            yy, xx = np.mgrid[0:h, 0:w_].astype(np.float32)
            r = np.sqrt(((xx - w_ / 2) / (w_ / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2) / math.sqrt(2)
            state['vig'] = (1 - a.vignette * np.clip(r, 0, 1) ** 2)[:, :, None]
            state['tint'] = np.array(parse_color(a.tint), np.float32) if a.tint != 'none' else None
        g = f.astype(np.float32)
        if a.sat != 1.0:
            hsv = cv2.cvtColor(f, cv2.COLOR_BGR2HSV).astype(np.float32)
            hsv[:, :, 1] = np.clip(hsv[:, :, 1] * a.sat, 0, 255)
            g = cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR).astype(np.float32)
        if state['tint'] is not None:
            g = g * (1 - a.tint_strength) + state['tint'] * a.tint_strength
        if a.grain > 0:
            rng = np.random.default_rng(k)
            n = rng.normal(0, 255 * a.grain, (h // 2, w_ // 2, 1)).astype(np.float32)
            g = g + cv2.resize(n, (w_, h), interpolation=cv2.INTER_LINEAR)[:, :, None]
        g = g * state['vig']
        return np.clip(g, 0, 255).astype(np.uint8)
    run_per_frame(a, fn)


def get_font(path, size, cache={}):
    key = (path, size)
    if key not in cache:
        if path and not os.path.exists(path):
            die(f'找不到字体文件：{path}')
        cands = [path] if path else FONT_CANDIDATES
        for p in cands:
            if p and os.path.exists(p):
                cache[key] = ImageFont.truetype(p, size)
                break
        else:
            die('电脑上没找到可用的中文字体，请用 --font 指定一个 .ttf / .ttc 字体文件')
    return cache[key]


HANGUL_RE = re.compile('[ᄀ-ᇿ㄰-㆏가-힣]')


def font_runs(text, size, path=None):
    """把一行字按文字种类切段：韩文段用带韩文的字体，其余（中文、英文）用默认字体。
    整行只用一种字体的话，中韩混排时总有一半显示成方框（微软雅黑没有韩文，Malgun 缺很多简体中文）。"""
    if path:
        return [(text, get_font(path, size))]
    hf = next((p for p in HANGUL_FONTS if os.path.exists(p)), None)
    if not hf or not HANGUL_RE.search(text or ''):
        return [(text, get_font(None, size))]
    runs, cur, kind = [], '', None
    for ch in text:
        k = 'h' if HANGUL_RE.match(ch) else ('s' if ch.isspace() else 'o')
        if k == 's':
            k = kind or 'o'   # 空格跟着前一段走
        if kind is not None and k != kind:
            runs.append((cur, kind))
            cur = ''
        cur, kind = cur + ch, k
    if cur:
        runs.append((cur, kind))
    return [(t, get_font(hf, size) if k == 'h' else get_font(None, size)) for t, k in runs]


def text_len(text, size, path=None):
    return sum(f.getlength(t) for t, f in font_runs(text, size, path))


def _baseline(runs):
    return max(f.getmetrics()[0] for _, f in runs)


def text_bbox(text, size, path=None):
    """和 ImageDraw.textbbox((0, 0), ...) 一样的坐标：从 (0, 0) 开始画时，字实际占的框。"""
    runs = font_runs(text, size, path)
    m = ImageDraw.Draw(Image.new('RGB', (8, 8)))
    base, x, box = _baseline(runs), 0.0, None
    for t, f in runs:
        b = m.textbbox((x, base), t, font=f, anchor='ls')
        box = b if box is None else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
        x += f.getlength(t)
    return box or (0, 0, 0, 0)


def draw_text(d, xy, text, size, fill, path=None, **kw):
    """一段一段接着画（每段用自己的字体），各段对齐到同一条基线。"""
    runs = font_runs(text, size, path)
    x, y = xy
    base = y + _baseline(runs)
    for t, f in runs:
        d.text((x, base), t, font=f, fill=fill, anchor='ls', **kw)
        x += f.getlength(t)


def wrap_text(text, size, max_w, path=None):
    """按画面宽度折行：中文、韩文逐字折，英文尽量整词折。"""
    lines, cur = [], ''
    for tok in re.findall(r'[A-Za-z0-9\'’.,!?()\-]+|\s+|.', text or ''):
        trial = cur + tok
        if cur and text_len(trial.rstrip(), size, path) > max_w:
            lines.append(cur.rstrip())
            cur = tok.lstrip()
        else:
            cur = trial
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


def text_patch(lines, size, fill=(255, 255, 255), bg=(0, 0, 0, 150), pad=0.35, font=None):
    """把几行字画成一块带半透明底的小图（RGBA），贴到画面上用。"""
    boxes = [text_bbox(t, size, font) for t in lines]
    p = int(size * pad)
    lh = int(size * 1.25)
    tw = int(max(b[2] for b in boxes)) if boxes else 1
    im = Image.new('RGBA', (tw + 2 * p, lh * len(lines) + 2 * p - (lh - size)), bg)
    d = ImageDraw.Draw(im)
    for k, t in enumerate(lines):
        draw_text(d, (p, p + k * lh), t, size, fill + (255,), font)
    return cv2.cvtColor(np.array(im), cv2.COLOR_RGBA2BGRA)


def paste(frame, patch, x, y):
    """把 RGBA 小图按透明度贴到画面 (x, y) 处（超出画面的部分裁掉）。"""
    H, W = frame.shape[:2]
    h, w_ = patch.shape[:2]
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w_, W), min(y + h, H)
    if x1 <= x0 or y1 <= y0:
        return frame
    sub = patch[y0 - y:y1 - y, x0 - x:x1 - x].astype(np.float32)
    al = sub[:, :, 3:4] / 255.0
    roi = frame[y0:y1, x0:x1].astype(np.float32)
    frame[y0:y1, x0:x1] = (roi * (1 - al) + sub[:, :, :3] * al).astype(np.uint8)
    return frame


def cmd_text(a):
    words = [x for x in a.words.split('|')] if '|' in a.words else a.words.split(',')
    if a.times:
        times = parse_times(a.times, a.bpm, a.fps)
    else:
        spb = 60.0 / a.bpm if a.bpm else die('没给 --times 时需要 --bpm 和 --each')
        times = [a.start + i * a.each * spb for i in range(len(words))]
    if len(times) != len(words):
        die(f'字有 {len(words)} 个，时间点有 {len(times)} 个，数量要一样')
    check_out(a.out, a.input, a.audio)
    base = Clip(a.input) if a.input else None
    W, H = out_size(a, base)
    dur = a.dur or (times[-1] + (a.each * 60.0 / a.bpm if a.bpm else 1.0) + 0.5)
    nf = int(round(dur * a.fps))
    bgs = [parse_color(c) for c in a.bg.split(',')]
    fixed = None if a.color == 'auto' else parse_color(a.color)[::-1]  # PIL 用 RGB

    def fit(word, size, max_w):  # 字太宽就缩小，保证不出画面
        while size > 8:
            bb = text_bbox(word, size, a.font)
            if bb[2] - bb[0] <= max_w:
                return size
            size = int(size * 0.9)
        return size

    # pop 模式：词从上到下排成"阶梯"，左右交错，字号逐个变大，互不重叠
    n = len(words)
    sizes = [int(H * (a.size_from + (a.size_to - a.size_from) * i / max(n - 1, 1))) for i in range(n)]
    sizes = [fit(wd, s, W * 0.9) for wd, s in zip(words, sizes)]
    total = sum(sizes) * 1.05
    if total > H * 0.88:
        r = H * 0.88 / total
        sizes = [max(8, int(s * r)) for s in sizes]
    centers, y = [], (H - sum(sizes) * 1.05) / 2
    for i, (wd, s) in enumerate(zip(words, sizes)):
        bb = text_bbox(wd, s, a.font)
        tw = bb[2] - bb[0]
        x0 = W * 0.06 if i % 2 == 0 else W * 0.94 - tw
        centers.append((x0 + tw / 2, y + s * 0.525))
        y += s * 1.05
    w = Writer(a.out, W, H, a.fps, a.audio, a.audio_start)
    for k in range(nf):
        t = k / a.fps
        if base is not None:
            frame = cover(base.frame(t), W, H)
            fg, stroke = fixed or (255, 255, 255), (0, 0, 0)
        else:
            c = bgs[(k // a.every) % len(bgs)]
            frame = np.full((H, W, 3), c, np.uint8)
            lum = 0.114 * c[0] + 0.587 * c[1] + 0.299 * c[2]
            fg, stroke = fixed or ((20, 20, 20) if lum > 140 else (245, 245, 245)), None
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        d = ImageDraw.Draw(img)
        shown = [i for i, tt in enumerate(times) if tt <= t]
        if a.mode == 'one':
            shown = shown[-1:]
        for i in shown:
            age = int(round((t - times[i]) * a.fps))
            pop = 1.0 + 0.3 * max(0, 1 - age / 3)  # 出现时先大 30%，3 帧内弹回
            if a.mode == 'one':
                size = fit(words[i], int(fit(words[i], int(H * a.size_to), W * 0.9) * pop), W * 0.96)
                cx, cy = W / 2, H / 2
            else:
                size = fit(words[i], int(sizes[i] * pop), W * 0.96)
                cx, cy = centers[i]
            bb = text_bbox(words[i], size, a.font)
            x, y = cx - (bb[0] + bb[2]) / 2, cy - (bb[1] + bb[3]) / 2
            x = min(max(x, W * 0.02 - bb[0]), W * 0.98 - bb[2])    # 弹出放大时也不出画面
            y = min(max(y, H * 0.02 - bb[1]), H * 0.98 - bb[3])
            kw = dict(stroke_width=max(2, size // 25), stroke_fill=stroke) if stroke else {}
            draw_text(d, (x, y), words[i], size, fg, a.font, **kw)
        w.write(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR))
    w.close()
    if base is not None:
        warn_frozen(base)


def cmd_pip(a):
    clip = getattr(a, '_clip', None) or Clip(a.input)
    check_out(a.out, a.input)
    if not a.fps:
        a.fps = round(clip.fps, 3) if not clip.still else 24
    W, H = out_size(a)
    start, nf = clip_frames(clip, a)
    ih = int(H * a.height) // 2 * 2
    iw = int(ih * 9 / 16) // 2 * 2
    rng = np.random.default_rng(1)
    jit = np.cumsum(rng.normal(0, 1, (nf, 2)), axis=0)
    jit = jit - np.linspace(0, 1, nf)[:, None] * jit[-1]  # 随机漂移但首尾回到原位
    jit = jit / (np.abs(jit).max() + 1e-6) * a.shake * H
    w = Writer(a.out, W, H, a.fps, None if clip.still else a.input, start)
    for k in range(nf):
        inner = cover(clip.frame(start + k / a.fps), iw, ih)
        frame = np.full((H, W, 3), parse_color(a.bg), np.uint8)
        x = int((W - iw) / 2 + jit[k, 0])
        y = int((H - ih) / 2 + jit[k, 1])
        x, y = min(max(x, 0), W - iw), min(max(y, 0), H - ih)
        frame[y:y + ih, x:x + iw] = inner
        w.write(frame)
    w.close()
    warn_frozen(clip)


def parse_len(s, bpm, fps):
    s = str(s).strip().lower()
    if s.endswith('拍') or s.endswith('b'):
        if not bpm:
            die(f'长度 "{s}" 按拍写，需要 --bpm')
        return float(s.rstrip('拍b')) * 60.0 / bpm
    if s.endswith('帧') or s.endswith('f'):
        return float(s.rstrip('帧f')) / fps
    return float(s.rstrip('秒s'))


FX_ZH = (('白闪', 'white'), ('黑闪', 'black'), ('黑场', 'black'), ('负片', 'invert'),
         ('故障', 'glitch'), ('冲推', 'punch'), ('推近', 'push'), ('慢推', 'push'))
FX_OK = ('white', 'black', 'invert', 'glitch', 'punch', 'push')


def half(s):
    """全角字符（输入法常打出来的 ／ ＋ ２ 等）换成半角，去掉两边空格。"""
    s = (s or '') if not isinstance(s, list) else ','.join(s)
    s = ''.join(chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in s)
    return s.replace('\u3000', ' ').strip()


def read_table(path, need=('素材', 'src'), hint='镜头,素材,入点,长度,效果'):
    """读镜头表 CSV（UTF-8 或 GBK）。返回 [(全角转半角后的行, 原样的行, 行号)]，跳过空行。"""
    if not os.path.exists(path):
        die(f'找不到镜头表：{path}')
    rows = None
    for enc in ('utf-8-sig', 'gbk'):
        try:
            with open(path, encoding=enc, newline='') as f:
                rdr = csv.DictReader(f)
                heads = [half(h) for h in (rdr.fieldnames or [])]
                if not any(n in heads for n in need):
                    die(f'镜头表第一行要是表头：{hint}（表头上面别留空行）')
                rows = []
                for r in rdr:  # 每行记下它在文件里的真实行号；素材名另存一份原样的
                    extra = [x for x in (r.pop(None, None) or []) if str(x).strip()]
                    if extra:
                        die(f'镜头表第 {rdr.line_num} 行比表头多了 {len(extra)} 格（{",".join(extra)}）：'
                            '效果之间用 + 连，别用逗号（如 推近+故障3）；备注请别写在表头以外的列')
                    raw = {half(k): (v.strip() if isinstance(v, str) else '') for k, v in r.items()}
                    rows.append(({half(k): half(v) for k, v in r.items()}, raw, rdr.line_num))
            break
        except UnicodeDecodeError:
            continue
    if rows is None:
        die('镜头表的文字编码认不出来：请在 Excel 里"另存为 → CSV UTF-8（逗号分隔）"')
    rows = [x for x in rows if any(x[0].values())]   # 跳过 Excel 留下的空行
    if not rows:
        die('镜头表是空的')
    return rows


def col(r, *names):
    for n in names:
        if n in r and r[n] not in (None, ''):
            return r[n].strip()
    return ''


def table_len(r, line, name, a):
    """镜头表里一行的"长度"换算成秒。"""
    ln = col(r, '长度', 'len').split('/')[0].replace('半拍', '0.5拍').replace(' ', '').lower()
    if (ln.endswith('拍') or ln.endswith('b')) and not a.bpm:
        die(f'镜头表第 {line} 行（{name}）的长度按拍写，需要同时给 --bpm')
    try:
        length = parse_len(ln, a.bpm, a.fps)
    except ValueError:
        die(f'镜头表第 {line} 行（{name}）的"长度"写成了"{col(r, "长度", "len")}"，要写成 4拍 / 0.5拍 / 6帧 / 0.93秒 这样')
    if not math.isfinite(length) or length <= 0 or length > 600:
        die(f'镜头表第 {line} 行（{name}）的"长度"要大于 0、不超过 600 秒（现在是"{col(r, "长度", "len")}"）')
    return length


def frame_counts(lengths, fps):
    """每刀帧数按累计时间取整，长片也不会越剪越偏。"""
    cum, done, out = 0.0, 0, []
    for L in lengths:
        cum += L
        n = int(round(cum * fps)) - done
        done += n
        out.append(n)
    return out


def card(W, H, lines_big, lines_small, color=(60, 60, 60), font=None):
    """示意卡：深色底 + 大字（镜头号）+ 小字（画面说明）。"""
    c = np.array(color, np.float32)
    yy = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    f = (c * (0.55 - 0.3 * yy)).clip(0, 255) * np.ones((1, W, 1), np.float32)   # 上亮下暗的渐变
    f = f.astype(np.uint8)
    big = int(H * 0.11)
    paste(f, text_patch(lines_big, big, bg=(0, 0, 0, 0), font=font), int(W * 0.05), int(H * 0.06))
    small = int(H * 0.05)
    wrapped = []
    for t in lines_small:
        wrapped += wrap_text(t, small, W * 0.86, font)
    if wrapped:
        p = text_patch(wrapped[:6], small, bg=(0, 0, 0, 110), font=font)
        paste(f, p, (W - p.shape[1]) // 2, int(H * 0.5 - p.shape[0] / 2))
    return f


def cmd_assemble(a):
    rows = read_table(a.list)
    base = os.path.dirname(os.path.abspath(a.list))
    cuts = []
    for r, raw, line in rows:
        i = line - 1
        src = col(raw, '素材', 'src')  # 素材名不做全角转换：中文文件名里的（）２ 要原样保留
        name = col(r, '镜头', 'S', 'shot') or f'第{len(cuts) + 1}刀'
        if not src and not a.placeholder:
            die(f'镜头表第 {i + 1} 行（表头算第 1 行）没有"素材"。表头要正好是：镜头,素材,入点,长度,效果'
                '（素材还没有的刀可以加 --placeholder 放占位卡）')
        length = table_len(r, i + 1, name, a)
        missing = None
        if src:
            cands = [src, half(src)]
            cands = [c if os.path.isabs(c) else os.path.join(base, c) for c in cands]
            src = next((c for c in cands if os.path.exists(c)), cands[0])
        if not src or not os.path.exists(src):
            if not a.placeholder:
                die(f'镜头表第 {i + 1} 行（{name}）的素材找不到：{src}（素材还没有的刀可以加 --placeholder 放占位卡）')
            missing, src = (os.path.basename(src) if src else '（没填素材）'), None
        inp_s = col(r, '入点', 'in') or '0'
        if not re.match(r'^(@\d+\.\d+\+?|\d+(\.\d+)?(b|拍|f|帧|s|秒)?)$', inp_s.lower()):
            die(f'镜头表第 {i + 1} 行（{name}）的"入点"写成了"{inp_s}"，要写成 0.5（秒）、2b（拍）或 6f（帧）这样')
        if (inp_s.startswith('@') or inp_s.lower().endswith(('b', '拍'))) and not a.bpm:
            die(f'镜头表第 {i + 1} 行（{name}）的入点按拍写，需要同时给 --bpm')
        fx_raw = col(r, '效果', 'fx').lower()
        fxs = fx_raw
        for zh, en in FX_ZH:
            fxs = fxs.replace(zh, en)  # 效果也可以写中文
        fx = [x for x in fxs.replace('，', '+').replace(',', '+').replace(' ', '').split('+') if x]
        bad = [e for e in fx if not e.startswith(FX_OK)]
        if bad:
            die(f'镜头表第 {i + 1} 行（{name}）的效果 "{"+".join(bad)}" 不认识。镜头表里只能写：白闪2 / 黑闪2 / 负片 / 故障 / 冲推 / 推近'
                '（数字是帧数）；别的效果先单独做成小视频再放进来')
        cuts.append(dict(src=src, missing=missing, desc=col(raw, '画面', '说明', '备注'),
                         inp=parse_time(inp_s, a.bpm, a.fps),
                         length=length, fx=fx, fx_raw=fx_raw, name=name, line=i + 1))
    check_out(a.out, a.audio, a.list, *[c['src'] for c in cuts if c['src']])
    for c, n in zip(cuts, frame_counts([c['length'] for c in cuts], a.fps)):  # 预检查和正式剪用同一个数
        c['nf'] = n
        if n <= 0:
            print(f'注意：镜头表第 {c["line"]} 行（{c["name"]}）不到 1 帧，会被跳过（长度太短，或者和前一刀合起来取整被吃掉了）')
    clips = {}
    for c in cuts:  # 开始写输出之前，先把每个素材打开试读，坏文件提前报出来
        if not c['src']:
            continue
        fn = os.path.basename(c['src'])
        try:
            if c['src'] not in clips:
                clips[c['src']] = Clip(c['src'])
            clip = clips[c['src']]
            clip.frame(c['inp'])
        except SystemExit:
            die(f'镜头表第 {c["line"]} 行（{c["name"]}）的素材 {fn} 打不开或读不出画面，可能文件坏了')
        if not clip.still and clip.incomplete:
            last = int(round((c['inp'] + (c['nf'] - 1) / a.fps) * clip.fps))  # 这一刀要用到的最后一帧
            if last > clip.n - 1:
                die(f'镜头表第 {c["line"]} 行（{c["name"]}）的素材 {fn} 不完整：写着 {clip.claimed} 帧，'
                    f'只有前 {clip.n / clip.fps:.2f} 秒读得出来，可能没下载完或没拷完')
    W, H = parse_size(a.size)
    w = Writer(a.out, W, H, a.fps, a.audio, a.audio_start)
    label0 = a.audio_start if a.label_start is None else a.label_start
    report, k0 = [], 0
    for c in cuts:
        nf = c['nf']
        if c['src']:
            clip = clips[c['src']]
            over0, bad0 = clip.overrun, (0 if clip.still else clip.badread)
        else:  # 素材还没有：放一张占位卡，片子照样能从头看到尾
            clip = None
            ph = card(W, H, [c['name']], ([c['desc']] if c['desc'] else []) + ['缺素材：' + c['missing']], font=a.font)
        lab = None
        if a.label:  # 左下角压镜头号 + 这一刀在歌里的起点（秒）
            lab = text_patch([f"{c['name']}  {label0 + k0 / a.fps:.2f}"], max(14, int(H * 0.034)), font=a.font)
        fx = c['fx']
        for j in range(nf):
            f = cover(clip.frame(c['inp'] + j / a.fps), W, H) if clip else ph.copy()
            for e in fx:
                digits = ''.join(ch for ch in e if ch.isdigit())
                n = int(digits or 2)
                head = (not digits) or j < n  # 负片 / 故障：不写数字是整刀，写数字只做开头几帧
                if e.startswith('white') and j < n:
                    f = np.full_like(f, 255)
                elif e.startswith('black') and j < n:
                    f = np.zeros_like(f)
                elif e.startswith('invert') and head:
                    f = 255 - f
                elif e.startswith('glitch') and head:
                    f = glitch_frame(f, j)
                elif e.startswith('punch'):
                    f = zoom(f, 1 + 0.15 * ease(min(j / 3, 1), 'out'))
                elif e.startswith('push'):
                    f = zoom(f, 1 + 0.06 * j / max(nf - 1, 1))
            if lab is not None:
                f = paste(np.ascontiguousarray(f), lab, int(W * 0.015), H - lab.shape[0] - int(H * 0.02))
            w.write(f)
        k0 += max(nf, 0)
        if clip is None:
            report.append(f"{c['name']}\t{nf} 帧\t占位卡（缺 {c['missing']}）\t{c['fx_raw']}")
            continue
        frozen = clip.overrun - over0
        badn = 0 if clip.still else clip.badread - bad0
        report.append(f"{c['name']}\t{nf} 帧\t{os.path.basename(c['src'])} @ {c['inp']:.2f}s\t{c['fx_raw']}"
                      + (f"\t注意：素材不够长，最后 {frozen} 帧是定格" if frozen else '')
                      + (f"\t注意：有 {badn} 帧读坏了，用前一帧代替" if badn else ''))
    w.close()
    print('镜头\t帧数\t来源\t效果')
    print('\n'.join(report))
    miss = [c['name'] for c in cuts if not c['src']]
    if miss:
        print(f'注意：{len(miss)} 刀还没有素材、放的是占位卡：' + '、'.join(miss))


def cmd_animatic(a):
    """节奏预览片：镜头表每刀一张示意卡，跟着原曲卡拍放。"""
    rows = read_table(a.list, need=('长度', 'len'), hint='镜头,长度,画面（可选：歌词,颜色,世界,制作）')
    palette = [(150, 190, 60), (200, 200, 200), (40, 40, 210), (60, 150, 210), (190, 90, 150), (160, 160, 40)]
    worlds, cuts = {}, []
    for r, raw, line in rows:
        name = col(r, '镜头', 'S', 'shot') or f'第{len(cuts) + 1}刀'
        length = table_len(r, line, name, a)
        cstr = col(r, '颜色', 'color')
        if cstr:
            color = parse_color(cstr)
        else:  # 没写颜色就按"世界"列分配一种颜色，同一个世界同一种颜色
            wd = col(raw, '世界', '角落', '段落', 'world')
            color = palette[worlds.setdefault(wd, len(worlds)) % len(palette)] if wd else (90, 90, 90)
        cuts.append(dict(name=name, length=length, color=color, desc=col(raw, '画面', '说明', '备注'),
                         lyric=col(raw, '歌词', 'lyric'), level=col(r, '制作', 'level')))
    check_out(a.out, a.audio, a.list)
    W, H = parse_size(a.size)
    w = Writer(a.out, W, H, a.fps, a.audio, a.audio_start)
    spb = 60.0 / a.bpm if a.bpm else None
    beat0 = a.audio_start if a.beat0 is None else a.beat0
    k0 = 0
    for c, nf in zip(cuts, frame_counts([c['length'] for c in cuts], a.fps)):
        if nf <= 0:
            print(f'注意：{c["name"]} 不到 1 帧，会被跳过（长度太短，或者和前一刀合起来取整被吃掉了）')
            continue
        big = [c['name'] + (f'  {c["level"]}' if c['level'] else '')]
        base = card(W, H, big, [c['desc']] if c['desc'] else [], c['color'], font=a.font)
        if c['lyric']:
            size = int(H * 0.045)
            p = text_patch(wrap_text(c['lyric'], size, W * 0.8, a.font)[:2],
                           size, fill=(255, 235, 120), bg=(0, 0, 0, 0), font=a.font)
            paste(base, p, (W - p.shape[1]) // 2, int(H * 0.78))
        paste(base, text_patch([f'{a.audio_start + k0 / a.fps:.2f}s'], int(H * 0.03), bg=(0, 0, 0, 120), font=a.font),
              int(W * 0.015), H - int(H * 0.07))
        t0 = a.audio_start + k0 / a.fps
        beats = []   # 这一刀里落着的整拍（按 --beat0 的拍子格算，有弱起也对得上）
        if spb:
            b0 = math.ceil((t0 - beat0) / spb - 1e-6)
            b1 = math.ceil((t0 + nf / a.fps - beat0) / spb - 1e-6)
            beats = [beat0 + b * spb for b in range(b0, b1)]
        r_ = max(4, int(H * 0.012))
        counters = {}
        for j in range(nf):
            f = base.copy()
            if spb:
                ts = t0 + j / a.fps
                if any(0 <= ts - bt < 2 / a.fps for bt in beats):  # 每拍开头两帧闪亮一下
                    f = cv2.addWeighted(f, 0.8, np.full_like(f, 255), 0.2, 0)
                done = sum(1 for bt in beats if bt <= ts + 1e-6)
                if len(beats) <= 16:   # 右下角的小圆点：这一刀里有几个拍点、走过了几个
                    x = W - int(W * 0.03) - len(beats) * r_ * 3
                    for b in range(len(beats)):
                        cv2.circle(f, (x + b * r_ * 3, H - int(H * 0.05)), r_, (255, 255, 255), -1 if b < done else 1, cv2.LINE_AA)
                else:                  # 拍子太多就写字，免得圆点排出画面
                    if done not in counters:
                        counters[done] = text_patch([f'{done} / {len(beats)} 拍'], int(H * 0.03), bg=(0, 0, 0, 120), font=a.font)
                    cp = counters[done]
                    paste(f, cp, W - cp.shape[1] - int(W * 0.015), H - int(H * 0.07))
            w.write(f)
        k0 += max(nf, 0)
    w.close()
    print(f'{len(cuts)} 刀，共 {k0 / a.fps:.2f} 秒')


def cmd_retime(a):
    """把编舞参考片段放慢 / 加快到歌的 BPM：输出第 0 秒 = 原片里小节第 1 拍，24 帧，不带声音。"""
    clip = getattr(a, '_clip', None) or Clip(a.input)
    if clip.still:
        die('retime 只用于视频')
    check_out(a.out, a.input)
    if a.src_bpm <= 0 or a.bpm <= 0:
        die('--src-bpm 和 --bpm 都要大于 0')
    rate = a.bpm / a.src_bpm   # 输出每过 1 秒，原片走过几秒
    nf = int(round(a.dur * a.fps))
    if nf <= 0:
        die('--dur 要大于 0（要做几秒）')
    need = a.from_ + nf / a.fps * rate
    if need > clip.dur + 1e-6:
        print(f'注意：要用到原片第 {need:.2f} 秒，原片只有 {clip.dur:.2f} 秒，最后会定格')
    W, H = out_size(a, clip)
    w = Writer(a.out, W, H, a.fps)
    cache = {}

    def get(i):
        i = min(max(i, 0), clip.n - 1)
        if i not in cache:
            cache[i] = clip.frame(i / clip.fps).astype(np.float32)
            for k in [k for k in cache if k < i - 2]:
                del cache[k]
        return cache[i]
    for k in range(nf):
        ts = (a.from_ + k / a.fps * rate) * clip.fps
        if a.blend:
            i = int(math.floor(ts))
            u = ts - i
            f = get(i) * (1 - u) + get(i + 1) * u
        else:
            f = get(int(round(ts)))
        w.write(cover(np.clip(f, 0, 255).astype(np.uint8), W, H))
    w.close()
    print(f'速度 {rate:.3f} 倍（{a.src_bpm:g} → {a.bpm:g} BPM）；输出第 0 秒 = 原片第 {a.from_:.2f} 秒；'
          f'输出一拍 {60 / a.bpm:.4f} 秒；用到原片 {a.from_:.2f}~{min(need, clip.dur):.2f} 秒')


def cmd_cutaudio(a):
    """截原曲片段（给对口型 / 参考音频用）。可以往前多截一点，让那句落在片段的第几秒。"""
    check_out(a.out, a.input)
    if not os.path.exists(a.input):
        die(f'找不到音乐文件：{a.input}')
    ext = os.path.splitext(a.out)[1].lower()
    if ext not in ('.wav', '.m4a', '.aac'):
        die(f'输出要存成 .wav 或 .m4a（你写的是 {ext or "没有扩展名"}）')
    raw = str(a._raw.get('start', ''))
    if raw.startswith('@') or raw.lower().endswith(('b', '拍')):  # 按拍写的起点：从第 1 小节第 1 拍算，不是从歌的第 0 秒
        if not a.bar1:
            print('注意：--start 按拍位写、但没给 --bar1（第 1 小节第 1 拍在歌里第几秒）；歌有弱起的话起点会差几拍')
        a.start += a.bar1
    if a.dur <= 0:
        die('--dur 要大于 0（截几秒）')
    s0 = a.start - (a.lead or 0.0)
    if s0 < 0:
        die(f'往前多截 {a.lead} 秒以后起点变成负数了（{s0:.2f} 秒）')
    total = media_dur(a.input)
    if total is not None and s0 >= total:
        die(f'起点第 {s0:.2f} 秒已经超过了歌的总长 {total:.2f} 秒')
    cmd = ['ffmpeg', '-y', '-v', 'error', '-ss', f'{s0:.3f}', '-t', f'{a.dur:.3f}', '-i', a.input, '-vn']
    cmd += ['-c:a', 'pcm_s16le', '-ar', '44100'] if a.out.lower().endswith('.wav') else ['-c:a', 'aac', '-b:a', '320k']
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    if subprocess.run(cmd + [a.out]).returncode:
        die('ffmpeg 截取失败（上面几行英文是原因）')
    got = media_dur(a.out) or a.dur
    if got < a.dur - 0.05:
        print(f'注意：歌到第 {total:.2f} 秒就结束了，只截到 {got:.2f} 秒')
    print(f'完成：{a.out}（{got:.2f} 秒）。片段第 0 秒 = 歌里第 {s0:.2f} 秒，'
          f'歌里第 {a.start:.2f} 秒落在片段第 {a.lead or 0:.2f} 秒；挑入点时：歌里第几秒 = {s0:.2f} + 素材第几秒')


def cmd_sheet(a):
    clip = getattr(a, '_clip', None) or Clip(a.input)
    if clip.still:
        die('sheet 只用于视频')
    if a.every_frame:
        i0 = int(round(a.start * clip.fps))
        i1 = int(round((a.end if a.end else clip.dur) * clip.fps))
        ts = [i / clip.fps for i in range(i0, min(i1, clip.n))]
    else:
        ts = [clip.dur * (i + 0.5) / a.count for i in range(a.count)]
    n = len(ts)
    tw = a.thumb
    th = int(tw * clip.size[1] / clip.size[0])
    cols = a.cols
    rows = (n + cols - 1) // cols
    sheet = np.full((rows * (th + 18), cols * tw, 3), 20, np.uint8)
    for i, t in enumerate(ts):
        r, c = divmod(i, cols)
        f = cv2.resize(clip.frame(t), (tw, th), interpolation=cv2.INTER_AREA)
        y = r * (th + 18)
        sheet[y + 18:y + 18 + th, c * tw:(c + 1) * tw] = f
        cv2.putText(sheet, f'#{int(round(t * clip.fps))} {t:.2f}s', (c * tw + 3, y + 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (80, 230, 255), 1)
    imwrite(a.out, sheet)
    print('完成：' + a.out)
    warn_frozen(clip)


# ---------------------------------------------------------------- 命令行
def main():
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(errors='replace')
        except Exception:
            pass
    P = argparse.ArgumentParser(description='MV 剪辑效果工具箱（不用 AI 视频的那部分活）',
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = P.add_subparsers(dest='cmd', required=True)

    def add(name, fn, helptext, single=False, out=True):
        p = sub.add_parser(name, help=helptext, description=helptext)
        p.set_defaults(fn=fn)
        if single:
            p.add_argument('input', help='输入视频或图片')
            p.add_argument('--from', dest='from_', type=tval, default=0.0, help='从输入的第几秒开始用')
            p.add_argument('--dur', type=tval, help='做几秒（输入是图片时必填）')
        if out:
            p.add_argument('-o', '--out', required=True, help='输出文件')
        p.add_argument('--fps', type=float, default=None if single else 24,
                       help='输出帧率（处理单个视频时默认跟原视频一样，其余默认 24）')
        p.add_argument('--size', help='输出大小，如 1280x720')
        if name != 'beats':
            p.add_argument('--bpm', type=float, help='每分钟多少拍（时间写成 4b、@2.1 时需要）')
        return p

    p = add('beats', cmd_beats, '节拍换算表：每个拍位是第几秒、第几帧', out=False)
    p.add_argument('--bpm', type=float, required=True, help='每分钟多少拍')
    p.add_argument('--bars', type=int, default=8, help='几个小节')
    p.add_argument('--per-bar', type=int, default=4, help='每小节几拍')
    p.add_argument('--start', type=tval, default=0.0, help='第 1 小节第 1 拍在第几秒')
    p.add_argument('--csv', help='另存成表格')

    p = add('montage', cmd_montage, '闪切蒙太奇：一组图片/视频按固定帧数、越切越快或按拍数拼起来')
    p.add_argument('files', nargs='*', help='素材文件（也可以用 --dir）')
    p.add_argument('--dir', help='素材文件夹（按文件名排序）')
    p.add_argument('--frames', type=int, default=2, help='每个素材几帧（默认 2）')
    p.add_argument('--accel', help='越切越快：起始帧数:结束帧数，如 9:3')
    p.add_argument('--beats', type=float, help='每个素材几拍，如 0.5（要配 --bpm）')
    p.add_argument('--drift', type=float, default=0.0, help='每张图内部轻微推近的幅度，如 0.03')
    p.add_argument('--loop', type=int, default=1, help='整组重复几遍')
    p.add_argument('--ghost', action='store_true', help='点名重影：每张快推 + 上一张残影 + 新一张虚影收回 + 切点红蓝错位（代替 --drift）')
    p.add_argument('--push', type=float, default=0.08, help='--ghost 时每张往里推多少（0.08 = 推 8%%）')
    p.add_argument('--push-last', type=float, help='--ghost 时最后一张推多少（收尾重音，比如 0.14）')
    p.add_argument('--centers', help='--ghost 时每张往哪推（脸的位置，画面比例），如 "0.46,0.42;0.46,0.4;0.3,0.45"；不够就循环用')
    p.add_argument('--audio', help='配上音乐')
    p.add_argument('--audio-start', type=tval, default=0.0, help='音乐从第几秒开始')

    p = add('kenburns', cmd_kenburns, '静图慢推 / 慢拉 / 平移（可加抠好的前景做两层视差）')
    p.add_argument('image', help='底图')
    p.add_argument('--dur', type=tval, default=3.0, help='做几秒')
    p.add_argument('--zoom-from', type=float, default=1.0, help='开始时放大几倍（不能小于 1）')
    p.add_argument('--zoom-to', type=float, default=1.12, help='结束时放大几倍；慢拉就把 from 写大、to 写 1.0')
    p.add_argument('--pan-from', default='0,0', help='起点中心偏移（画面比例），负数要用等号：--pan-from=-0.05,0')
    p.add_argument('--pan-to', default='0,0', help='终点中心偏移')
    p.add_argument('--ease', default='inout', choices=['inout', 'in', 'out', 'linear'], help='快慢曲线：inout 两头慢中间快，in 越来越快，out 越来越慢，linear 匀速')
    p.add_argument('--fg', help='前景透明 PNG（抠好的人/物），做两层视差')
    p.add_argument('--fg-mult', type=float, default=1.8, help='前景运动是背景的几倍')

    p = add('interleave', cmd_interleave, 'AB 交替闪切：两段素材每 N 帧交替一次，两边动作都按时间连续')
    p.add_argument('--a', required=True, help='A 段素材（视频或图片）')
    p.add_argument('--b', required=True, help='B 段素材（视频或图片）')
    p.add_argument('--every', type=int, default=3, help='每几帧换一次（默认 3）')
    p.add_argument('--dur', type=tval, default=1.5, help='做几秒')
    p.add_argument('--a-from', type=tval, default=0.0, help='A 从第几秒开始用')
    p.add_argument('--b-from', type=tval, default=0.0, help='B 从第几秒开始用')

    p = add('strobe', cmd_strobe, '明暗频闪：亮 N 帧、暗 M 帧交替', single=True)
    p.add_argument('--on', type=int, default=3, help='亮几帧')
    p.add_argument('--off', type=int, default=2, help='暗几帧')
    p.add_argument('--dark', type=float, default=0.15, help='暗帧亮度（0~1）')
    p.add_argument('--start', type=tval, default=0.0, help='从第几秒开始闪')
    p.add_argument('--end', type=tval, help='到第几秒停')

    p = add('color', cmd_color, '色彩频闪：每 N 帧换一种颜色（警灯、字卡底色、调色版本）', single=True)
    p.add_argument('--colors', default='red,blue', help='颜色，逗号分隔；可用颜色名或 #RRGGBB')
    p.add_argument('--every', type=int, default=3, help='每几帧换一种颜色')
    p.add_argument('--strength', type=float, default=0.45, help='颜色盖上去的浓度（0~1）')
    p.add_argument('--start', type=tval, default=0.0, help='从第几秒开始变色')
    p.add_argument('--end', type=tval, help='到第几秒停')

    for name, fn, h in [('invert', cmd_invert, '负片闪帧：指定时间点插入几帧反色'),
                        ('glitch', cmd_glitch, '故障效果：指定时间点几帧红蓝错位 + 横条错位')]:
        p = add(name, fn, h, single=True)
        p.add_argument('--at', required=True, help='时间点，逗号分隔，如 1.2,2.5 或 4b,8b')
        p.add_argument('--frames', type=int, default=2, help='每个时间点持续几帧')
        if name == 'glitch':
            p.add_argument('--amount', type=float, default=0.02, help='红蓝错位幅度（画面宽度比例）')
            p.add_argument('--seed', type=int, default=0, help='随机种子：换个数字，错位条的样子就不同')

    p = add('flash', cmd_flash, '白闪 / 黑闪：指定时间点盖一层白（或黑），再淡掉', single=True)
    p.add_argument('--at', required=True, help='时间点，逗号分隔，如 1.2,2.5 或 4b,8b 或 @2.1,@3.1')
    p.add_argument('--frames', type=int, default=4, help='总共几帧')
    p.add_argument('--hold', type=int, default=1, help='其中前几帧是纯色')
    p.add_argument('--color', default='white', help='闪什么颜色；black 就是黑场')

    p = add('punch', cmd_punch, '卡点冲推：在重拍上猛地放大（带动态模糊）', single=True)
    p.add_argument('--at', required=True, help='时间点，逗号分隔，如 1.2,2.5 或 4b,8b 或 @2.1,@3.1')
    p.add_argument('--scale', type=float, default=1.2, help='每次推近多少：1.2 = 放大到 1.2 倍；默认一次比一次近（累计），加 --reset 每次从原大小推')
    p.add_argument('--ramp', type=int, default=3, help='推的过程几帧')
    p.add_argument('--reset', action='store_true', help='每次都从原大小重新推（默认一次比一次近）')

    p = add('whip', cmd_whip, '甩镜转场：A 结尾甩出、B 开头甩入')
    p.add_argument('--a', required=True, help='甩出的那段（A）')
    p.add_argument('--b', required=True, help='甩入的那段（B）')
    p.add_argument('--a-from', type=tval, default=0.0, help='A 从第几秒开始用')
    p.add_argument('--b-from', type=tval, default=0.0, help='B 从第几秒开始用')
    p.add_argument('--dur-a', type=tval, default=1.0, help='A 段用几秒（甩出包含在内）')
    p.add_argument('--dur-b', type=tval, default=1.0, help='B 段用几秒（甩入包含在内）')
    p.add_argument('--frames', type=int, default=5, help='甩出 / 甩入各几帧')
    p.add_argument('--dir', default='right', choices=['left', 'right', 'up', 'down'], help='镜头往哪边甩')

    p = add('spin', cmd_spin, '滚转：在指定时间段把画面转过几度（自动放大，四角不露黑）', single=True)
    p.add_argument('--deg', type=float, default=90, help='转到几度（正数逆时针）')
    p.add_argument('--from-deg', type=float, default=0.0, help='开始时是几度；做"转入"：--from-deg -90 --deg 0 --ease out')
    p.add_argument('--start', type=tval, default=0.0, help='第几秒开始转')
    p.add_argument('--length', type=tval, default=0.4, help='转多少秒')
    p.add_argument('--ease', default='in', choices=['inout', 'in', 'out', 'linear'], help='甩出用 in，甩入用 out')

    p = add('look', cmd_look, '质感层：暗角 + 降饱和 + 偏色（颗粒默认不加，AI 素材本身有颗粒）', single=True)
    p.add_argument('--grain', type=float, default=0.0,
                   help='颗粒强度（默认 0 不加：AI 生成的素材本身就有颗粒，再加会脏；实拍或很干净的 CG 素材才加，比如 0.05）')
    p.add_argument('--vignette', type=float, default=0.45, help='暗角强度，0 表示不加')
    p.add_argument('--sat', type=float, default=0.85, help='饱和度倍数：1 不变，小于 1 颜色变淡')
    p.add_argument('--tint', default='teal', help='偏什么色；none 表示不偏色')
    p.add_argument('--tint-strength', type=float, default=0.08, help='偏色浓度（0~1）')

    p = add('text', cmd_text, '动态歌词字 / 字卡：词按拍弹出（可叠在视频上，或纯色底色频闪）')
    p.add_argument('--words', required=True, help='词，用逗号分隔（词里有逗号就用 | 分隔）')
    p.add_argument('--times', help='每个词出现的时间，逗号分隔（不给就用 --bpm + --each）')
    p.add_argument('--each', type=float, default=1.0, help='每个词间隔几拍')
    p.add_argument('--start', type=tval, default=0.0, help='第一个词在第几秒')
    p.add_argument('--input', help='叠在这个视频/图片上（不给就是纯色底）')
    p.add_argument('--bg', default='red', help='纯色底的颜色，逗号分隔多个就会频闪')
    p.add_argument('--every', type=int, default=2, help='底色每几帧换一次')
    p.add_argument('--color', default='auto', help='字的颜色；auto=按底色自动选黑或白，叠在视频上时用白字黑描边')
    p.add_argument('--mode', default='pop', choices=['pop', 'one'], help='pop=词逐个留在画面上；one=一次只显示一个大字')
    p.add_argument('--size-from', type=float, default=0.12, help='第一个词字号（画面高度的比例）')
    p.add_argument('--size-to', type=float, default=0.3, help='最后一个词字号')
    p.add_argument('--font', help='字体文件路径（默认微软雅黑粗体；有韩文时自动换成带韩文的字体）')
    p.add_argument('--dur', type=tval, help='总共几秒（不填就到最后一个词后面一点）')
    p.add_argument('--audio', help='配上音乐')
    p.add_argument('--audio-start', type=tval, default=0.0, help='音乐从第几秒开始')

    p = add('pip', cmd_pip, '竖屏画中画：16:9 黑底中间嵌一个手机竖屏画面（可轻微手抖）', single=True)
    p.add_argument('--height', type=float, default=0.92, help='竖屏小画面占画面高度的比例')
    p.add_argument('--shake', type=float, default=0.01, help='手抖幅度（画面高度比例，0 表示不抖）')
    p.add_argument('--bg', default='black', help='底色')

    p = add('assemble', cmd_assemble, '按镜头表剪辑：读 CSV（镜头,素材,入点,长度,效果），按拍卡点拼成片')
    p.add_argument('list', help='镜头表 CSV')
    p.add_argument('--audio', help='配上原曲')
    p.add_argument('--audio-start', type=tval, default=0.0, help='音乐从第几秒开始（副歌第一拍在歌里的秒数）')
    p.add_argument('--label', action='store_true', help='镜头号版：每刀左下角压"镜头号 + 这刀在歌里的起点秒数"（给返修用，成片别加）')
    p.add_argument('--label-start', type=tval, help='镜头号里的秒数从几开始算（默认等于 --audio-start）')
    p.add_argument('--placeholder', action='store_true', help='素材还没有的刀放占位卡（镜头号 + "画面"列 + 缺哪个文件），不报错')
    p.add_argument('--font', help='字体文件（默认微软雅黑；有韩文时自动换成带韩文的字体）')
    p.set_defaults(size='1280x720')

    p = add('animatic', cmd_animatic, '节奏预览片：镜头表每刀一张示意卡（镜头号、画面、歌词、世界配色），跟着原曲卡拍放')
    p.add_argument('list', help='镜头表 CSV：必须有 镜头,长度；可选 画面,歌词,颜色,世界,制作')
    p.add_argument('--audio', help='配上原曲')
    p.add_argument('--audio-start', type=tval, default=0.0, help='第一刀在歌里第几秒（整首从头做就是 0）')
    p.add_argument('--beat0', type=tval, help='歌里任意一个整拍的秒数（比如第 1 小节第 1 拍）；拍点闪烁和小圆点按它对齐，默认等于 --audio-start')
    p.add_argument('--font', help='字体文件（默认微软雅黑；有韩文时自动换成带韩文的字体）')
    p.set_defaults(size='1280x720')

    p = add('retime', cmd_retime, '舞蹈参考片对齐歌速：放慢 / 加快到 --bpm（歌的速度），--from 写原片里小节第 1 拍的秒数，24 帧，去掉声音', single=True)
    p.add_argument('--src-bpm', type=float, required=True, help='原片（编舞视频）的 BPM')
    p.add_argument('--blend', action='store_true', help='相邻两帧按比例混合（放慢很多时更顺，但快动作会有重影）')
    p.set_defaults(fps=24, dur=5.0)
    for act in p._actions:  # 通用参数的说明换成这个命令自己的意思
        act.help = {'fps': '输出帧率（默认 24）', 'dur': '输出几秒（默认 5）',
                    'from_': '原片里小节第 1 拍在第几秒（输出第 0 秒就从这里开始）'}.get(act.dest, act.help)

    p = add('cutaudio', cmd_cutaudio, '截原曲片段：给对口型 / 参考音频用（.wav 或 .m4a）')
    p.add_argument('input', help='原曲')
    p.add_argument('--start', type=tval, required=True, help='要的那句在歌里第几秒开始')
    p.add_argument('--dur', type=tval, default=5.0, help='截几秒（默认 5）')
    p.add_argument('--lead', type=tval, default=0.0, help='往前多截几秒（想让那句落在片段第 2.4 秒，就写 2.4）')
    p.add_argument('--bar1', type=float, default=0.0, help='--start 按拍位写（@11.1）时：歌里第 1 小节第 1 拍在第几秒（有弱起的歌一定要给）')

    p = add('sheet', cmd_sheet, '联系表：从视频里均匀抽帧拼成一张图，用来检查效果', single=False)
    p.add_argument('input', help='要检查的视频')
    p.add_argument('--count', type=int, default=24, help='均匀抽几帧')
    p.add_argument('--cols', type=int, default=6, help='每行几格')
    p.add_argument('--thumb', type=int, default=240, help='每格多宽（像素）')
    p.add_argument('--every-frame', action='store_true', help='逐帧排出来（检查 2 帧白闪、每 3 帧交替时用）')
    p.add_argument('--start', type=tval, default=0.0, help='逐帧模式从第几秒开始')
    p.add_argument('--end', type=tval, help='逐帧模式到第几秒')

    a = P.parse_args()
    if a.cmd == 'sheet':  # 6f 这类写法按视频自己的帧率换算
        a._clip = Clip(a.input)
        a.fps = a._clip.fps
    if a.cmd in ('assemble', 'animatic') and not a.size:
        a.size = '1280x720'
    if a.cmd == 'retime' and not a.bpm:
        P.error('retime 要用 --bpm 写目标速度（歌的 BPM），--src-bpm 写原片的 BPM')
    if getattr(a, 'fps', 1) is None:  # 处理单个素材：帧率跟原视频一样
        src = getattr(a, 'input', None)
        c = Clip(src) if src else None
        a._clip = c  # 后面直接用这个，不再重复打开
        a.fps = round(c.fps, 3) if (c and not c.still) else 24
    a._raw = {k: getattr(a, k, None) for k in TIME_ATTRS}
    for k in TIME_ATTRS:  # 4b / @2.1 / 6f 换算成秒
        v = getattr(a, k, None)
        if isinstance(v, str):
            setattr(a, k, parse_time(v, getattr(a, 'bpm', None), a.fps or 24))
    a.fn(a)


if __name__ == '__main__':
    main()
