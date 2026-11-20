#!/usr/bin/env python3
"""
Generate a lively, story-driven animated demo for tmem v0.4.0.
Demonstrates the complete workflow in 4 visual acts:
  Act 1: Context Compaction Amnesia vs tmem Snapshot & Timeline Capsule
  Act 2: Inline Compact Roadmap + Model-Driven L0->L3 / Full History Recall
  Act 3: L1 Co-change, L2 Same-Origin Error Rescue & <5ms AST Guard Shield
  Act 4: Verified Benchmarks & One-Line Install

Outputs:
  - docs/assets/tmem-demo.mp4 (H.264)
  - docs/assets/tmem-demo.gif (Optimized palette GIF)
"""
import math
import os
import shutil
import subprocess
from PIL import Image, ImageDraw, ImageFont

WIDTH = 1200
HEIGHT = 700
FPS = 24
TOTAL_SECONDS = 16.0
TOTAL_FRAMES = int(FPS * TOTAL_SECONDS)

# Catppuccin Macchiato inspired palette
BG = (20, 22, 34)
CARD = (30, 32, 48)
CARD_ALT = (36, 39, 58)
CARD_DARK = (24, 26, 39)
BORDER = (62, 67, 94)
TEXT = (226, 232, 250)
MUTED = (138, 145, 175)
DIM = (90, 96, 124)

BLUE = (138, 173, 244)
GREEN = (166, 218, 149)
RED = (237, 135, 150)
YELLOW = (238, 212, 159)
PURPLE = (198, 160, 246)
CYAN = (139, 213, 202)
ORANGE = (245, 169, 127)

FONT_CJK_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_CJK_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"

# index=2 -> Noto Sans CJK SC, index=7 -> Noto Sans Mono CJK SC
F_BANNER = ImageFont.truetype(FONT_CJK_BOLD, 24, index=2)
F_TITLE = ImageFont.truetype(FONT_CJK_BOLD, 19, index=2)
F_SUB = ImageFont.truetype(FONT_CJK_BOLD, 16, index=2)
F_BODY = ImageFont.truetype(FONT_CJK_REG, 15, index=2)
F_BODY_B = ImageFont.truetype(FONT_CJK_BOLD, 15, index=2)
F_SMALL = ImageFont.truetype(FONT_CJK_REG, 13, index=2)
F_SMALL_B = ImageFont.truetype(FONT_CJK_BOLD, 13, index=2)
F_MONO = ImageFont.truetype(FONT_CJK_REG, 14, index=7)
F_MONO_B = ImageFont.truetype(FONT_CJK_BOLD, 14, index=7)
F_BIG = ImageFont.truetype(FONT_CJK_BOLD, 34, index=2)


def clamp01(v):
    return max(0.0, min(1.0, v))


def ease_out(t):
    t = clamp01(t)
    return 1.0 - (1.0 - t) ** 3


def blend(c1, c2, t):
    t = clamp01(t)
    return tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))


def draw_pill(draw, x, y, text, font, fg, bg, border_col=None, px=12, py=5, r=8):
    w = int(draw.textlength(text, font=font)) + px * 2
    h = int(font.size * 1.45) + py * 2
    draw.rounded_rectangle([x, y, x + w, y + h], radius=r, fill=bg, outline=border_col or fg, width=1)
    draw.text((x + px, y + py - 1), text, font=font, fill=fg)
    return w, h


def draw_robot(draw, cx, cy, mood="happy", scale=1.0, phase=0.0):
    """Draw a cute vector robot mascot without relying on system emoji fonts."""
    s = scale
    head_col = CYAN if mood != "confused" else ORANGE
    if mood == "hurt":
        head_col = RED

    # Antenna bobbing
    bob = math.sin(phase * 6.0) * 3 * s if mood != "happy" else 0
    draw.line([cx, cy - 22 * s, cx, cy - 34 * s + bob], fill=head_col, width=max(2, int(3 * s)))
    draw.ellipse([cx - 4 * s, cy - 38 * s + bob, cx + 4 * s, cy - 30 * s + bob], fill=YELLOW)

    # Head box
    draw.rounded_rectangle(
        [cx - 24 * s, cy - 22 * s, cx + 24 * s, cy + 18 * s],
        radius=max(4, int(9 * s)),
        fill=CARD_ALT,
        outline=head_col,
        width=max(2, int(2.5 * s)),
    )
    # Ears
    draw.rounded_rectangle([cx - 30 * s, cy - 10 * s, cx - 24 * s, cy + 6 * s], radius=3, fill=head_col)
    draw.rounded_rectangle([cx + 24 * s, cy - 10 * s, cx + 30 * s, cy + 6 * s], radius=3, fill=head_col)

    # Eyes & Mouth
    if mood == "confused":
        # Dizzy X / O eyes + wavy mouth + floating '?'
        draw.line([cx - 14 * s, cy - 10 * s, cx - 6 * s, cy - 2 * s], fill=RED, width=2)
        draw.line([cx - 6 * s, cy - 10 * s, cx - 14 * s, cy - 2 * s], fill=RED, width=2)
        draw.ellipse([cx + 5 * s, cy - 11 * s, cx + 15 * s, cy - 1 * s], outline=YELLOW, width=2)
        draw.arc([cx - 10 * s, cy + 3 * s, cx + 10 * s, cy + 14 * s], 180, 360, fill=ORANGE, width=2)
        # Floating question mark
        q_y = cy - 46 * s + math.sin(phase * 4.0) * 4
        draw.text((cx + 22 * s, q_y), "?", font=F_TITLE, fill=YELLOW)
    elif mood == "thinking":
        # Focused scanning eyes
        scan = math.sin(phase * 5.0) * 3 * s
        draw.ellipse([cx - 14 * s + scan, cy - 9 * s, cx - 6 * s + scan, cy - 1 * s], fill=BLUE)
        draw.ellipse([cx + 6 * s + scan, cy - 9 * s, cx + 14 * s + scan, cy - 1 * s], fill=BLUE)
        draw.line([cx - 7 * s, cy + 8 * s, cx + 7 * s, cy + 8 * s], fill=CYAN, width=2)
    else:
        # Cool happy visor eyes + smile
        draw.rounded_rectangle([cx - 16 * s, cy - 10 * s, cx - 4 * s, cy - 2 * s], radius=2, fill=GREEN)
        draw.rounded_rectangle([cx + 4 * s, cy - 10 * s, cx + 16 * s, cy - 2 * s], radius=2, fill=GREEN)
        draw.arc([cx - 9 * s, cy + 1 * s, cx + 9 * s, cy + 12 * s], 15, 165, fill=GREEN, width=2)


def draw_check(draw, x, y, size=14, col=GREEN):
    draw.line([x, y + size * 0.55, x + size * 0.38, y + size * 0.9, x + size, y + size * 0.15], fill=col, width=3)


def draw_cross(draw, x, y, size=12, col=RED):
    draw.line([x, y, x + size, y + size], fill=col, width=3)
    draw.line([x + size, y, x, y + size], fill=col, width=3)


def draw_lightning(draw, x, y, s=1.0, col=YELLOW):
    pts = [
        (x + 6 * s, y),
        (x - 2 * s, y + 10 * s),
        (x + 4 * s, y + 10 * s),
        (x - 2 * s, y + 22 * s),
        (x + 11 * s, y + 8 * s),
        (x + 4 * s, y + 8 * s),
    ]
    draw.polygon(pts, fill=col)


def draw_shield(draw, cx, cy, r=28, col=BLUE, pulse=0.0):
    pts = [
        (cx, cy - r),
        (cx + r * 0.9, cy - r * 0.65),
        (cx + r * 0.85, cy + r * 0.2),
        (cx, cy + r),
        (cx - r * 0.85, cy + r * 0.2),
        (cx - r * 0.9, cy - r * 0.65),
    ]
    if pulse > 0:
        draw.polygon(pts, fill=blend(CARD, col, 0.25 + 0.15 * pulse), outline=col, width=3)
    else:
        draw.polygon(pts, fill=blend(CARD, col, 0.18), outline=col, width=3)
    draw_check(draw, cx - 8, cy - 8, 16, CYAN)


def draw_scissors(draw, cx, cy, col=RED, snip=0.0):
    ang = 0.25 + 0.35 * abs(math.sin(snip * 6.0))
    # Two blades
    for sign in (-1, 1):
        bx = cx + math.cos(sign * ang) * 20
        by = cy + math.sin(sign * ang) * 20
        draw.line([cx - 10, cy - sign * 8, bx, by], fill=col, width=3)
    draw.ellipse([cx - 17, cy - 14, cx - 7, cy - 4], outline=col, width=2)
    draw.ellipse([cx - 17, cy + 4, cx - 7, cy + 14], outline=col, width=2)


def draw_header(draw, sec, act_idx):
    draw.rectangle([0, 0, WIDTH, 64], fill=CARD_DARK)
    draw.line([0, 64, WIDTH, 64], fill=BORDER, width=1)

    # Brand Badge
    draw.rounded_rectangle([24, 14, 118, 50], radius=8, fill=blend(CARD, BLUE, 0.2), outline=BLUE, width=2)
    draw.text((38, 17), "tmem", font=F_BANNER, fill=BLUE)

    draw.text((134, 19), "DSH 防失忆工程记忆引擎 · 全流程演示", font=F_TITLE, fill=TEXT)

    # Act stepper
    acts = ["1 压缩留存", "2 按需查历史", "3 报错与防毒", "4 实测战绩"]
    sx = 560
    for i, label in enumerate(acts):
        active = i == act_idx
        done = i < act_idx
        fg = BG if active else (GREEN if done else MUTED)
        bg = CYAN if active else (blend(CARD, GREEN, 0.12) if done else CARD_ALT)
        bd = CYAN if active else (GREEN if done else BORDER)
        w, _ = draw_pill(draw, sx, 16, label, F_SMALL_B, fg, bg, bd, px=9, py=5, r=6)
        sx += w + 8

    # Top-right fixed counter
    draw_pill(draw, WIDTH - 156, 16, "LLM 总结: 0 tok", F_SMALL_B, GREEN, blend(CARD, GREEN, 0.12), GREEN, px=10, py=5, r=6)


def draw_footer(draw, sec):
    draw.rectangle([0, HEIGHT - 34, WIDTH, HEIGHT], fill=CARD_DARK)
    draw.line([0, HEIGHT - 34, WIDTH, HEIGHT - 34], fill=BORDER, width=1)
    # Timeline bar
    pct = clamp01(sec / TOTAL_SECONDS)
    draw.rectangle([0, HEIGHT - 36, int(WIDTH * pct), HEIGHT - 34], fill=CYAN)
    draw.text((24, HEIGHT - 26), "github.com/kentzjh-cmd/tmem  |  npm: dsh-tmem v0.4.0  |  纯 Node.js ESM 零依赖", font=F_SMALL, fill=MUTED)
    draw.text((WIDTH - 340, HEIGHT - 26), "$ dsh plugin add npm:dsh-tmem", font=F_MONO_B, fill=BLUE)


def render_act1(draw, t, sec):
    """Act 1 (0..4.2s): Context Explosion -> Vanilla Amnesia vs tmem Snapshot + Inline Signpost."""
    # Section Banner: Animated Context Tank filling to trigger compact!
    tank_fill = clamp01(0.58 + (t / 1.4) * 0.40) if t < 1.5 else (0.22 if t > 2.2 else 0.98)
    draw.rounded_rectangle([28, 78, WIDTH - 28, 146], radius=10, fill=CARD, outline=BORDER, width=1)
    draw.text((46, 88), "对话场景：正在修 Auth 模块 + 跑了 18KB 测试日志，上下文快爆了！", font=F_SUB, fill=YELLOW)

    # Progress Tank Bar
    bx, by, bw, bh = 46, 116, 620, 18
    draw.rounded_rectangle([bx, by, bx + bw, by + bh], radius=6, fill=CARD_DARK, outline=BORDER, width=1)
    bar_col = GREEN if tank_fill < 0.7 else (ORANGE if tank_fill < 0.9 else RED)
    draw.rounded_rectangle([bx + 2, by + 2, bx + 2 + int((bw - 4) * tank_fill), by + bh - 2], radius=4, fill=bar_col)
    status_txt = f"Context 占用: {int(tank_fill * 100)}%"
    if t >= 1.25 and t <= 2.35:
        status_txt = "Context 98% -> 触发 COMPACT 压缩 & 超长日志截断！"
    elif t > 2.35:
        status_txt = "压缩完成！工作窗口重新腾空，关键在「前面聊过的去哪了？」"
    draw.text((685, 114), status_txt, font=F_SMALL_B, fill=RED if (1.25 <= t <= 2.35) else TEXT)

    # Split screen: Left (Vanilla) vs Right (tmem)
    col_w = 560
    lx, rx = 28, 612
    top, h = 160, 492

    # --- LEFT CARD: Vanilla Compact ---
    draw.rounded_rectangle([lx, top, lx + col_w, top + h], radius=12, fill=CARD, outline=RED if t >= 1.6 else BORDER, width=2 if t >= 1.6 else 1)
    draw.rectangle([lx, top, lx + col_w, top + 40], fill=CARD_ALT)
    draw_cross(draw, lx + 18, top + 14, 12, RED)
    draw.text((lx + 40, top + 9), "没装 tmem：一压缩就“断片失忆”", font=F_SUB, fill=RED)

    steps_left = [
        (0.2, "[Turn 1] 定死架构规则：JWT leewaySec = 30s", CYAN, True),
        (0.5, "[Turn 3] 瞎逛读错文件 temp.txt（废操作）", MUTED, True),
        (0.8, "[Turn 4] edit jwt_guard.ts（改对核心 3 行）", GREEN, True),
        (1.1, "[Turn 6] 测试日志 18KB（正中间报错 ERR_TOKEN_DRIFT）", YELLOW, True),
    ]
    sy = top + 58
    wiped = t >= 1.8
    for ta, msg, col, gets_wiped in steps_left:
        if t >= ta:
            if wiped:
                draw.text((lx + 22, sy), msg, font=F_BODY, fill=DIM)
                tw = int(draw.textlength(msg, font=F_BODY))
                draw.line([lx + 20, sy + 11, lx + 24 + tw, sy + 11], fill=RED, width=2)
                draw.text((lx + col_w - 86, sy + 2), "被抹掉!", font=F_SMALL_B, fill=RED)
            else:
                draw.text((lx + 22, sy), msg, font=F_BODY, fill=col)
        sy += 38

    if 1.3 <= t <= 2.4:
        draw_scissors(draw, lx + col_w - 50, top + 120, RED, snip=t)

    if t >= 2.1:
        draw.rounded_rectangle([lx + 20, top + 225, lx + col_w - 20, top + 315], radius=10, fill=CARD_DARK, outline=RED, width=1)
        draw.text((lx + 34, top + 235), "压缩后仅剩一句模糊摘要：", font=F_SMALL_B, fill=RED)
        draw.text((lx + 34, top + 260), '"User worked on auth tokens and ran some tests."', font=F_MONO, fill=MUTED)
        draw.text((lx + 34, top + 286), "参数没了、哪行改了没了、报错原文也被截断了...", font=F_SMALL, fill=ORANGE)

    if t >= 2.7:
        draw_robot(draw, lx + 78, top + 408, mood="confused", scale=1.05, phase=sec)
        draw.rounded_rectangle([lx + 132, top + 345, lx + col_w - 20, top + 468], radius=10, fill=CARD_ALT, outline=ORANGE, width=1)
        draw.text((lx + 148, top + 357), "失忆的 Agent：", font=F_BODY_B, fill=ORANGE)
        draw.text((lx + 148, top + 382), "“刚定几秒来着？哪行报错来着？”", font=F_BODY, fill=TEXT)
        draw.text((lx + 148, top + 408), "只好重新满仓库 read 文件、重踩坑...", font=F_BODY, fill=RED)
        draw.text((lx + 148, top + 435), "浪费 Token +++  |  准确率暴跌", font=F_SMALL_B, fill=RED)

    # --- RIGHT CARD: With tmem ---
    draw.rounded_rectangle([rx, top, rx + col_w, top + h], radius=12, fill=CARD, outline=GREEN if t >= 2.0 else BORDER, width=2 if t >= 2.0 else 1)
    draw.rectangle([rx, top, rx + col_w, top + 40], fill=CARD_ALT)
    draw_check(draw, rx + 16, top + 12, 14, GREEN)
    draw.text((rx + 40, top + 9), "装了 tmem：压缩瞬间自动快照 + 内联路标", font=F_SUB, fill=GREEN)

    if t >= 1.35:
        # Pipeline Step A: Backward slice & save to .tmem/episodes/
        draw.rounded_rectangle([rx + 20, top + 56, rx + col_w - 20, top + 184], radius=10, fill=CARD_DARK, outline=BLUE, width=1)
        draw.text((rx + 34, top + 66), "1. 监听 compaction 事件，不用大模型就地剪辑：", font=F_BODY_B, fill=BLUE)
        draw.text((rx + 34, top + 94), "- 咔嚓剔除 Turn 3 闲逛废步 & 撤回调试代码", font=F_SMALL, fill=MUTED)
        draw.text((rx + 34, top + 118), "- 完整封存 .tmem/episodes/ep_02（含 Diff & 18KB 原文）", font=F_SMALL_B, fill=CYAN)
        draw.text((rx + 34, top + 142), "- 自动接上时间线: Phase #1 --> Phase #2 (prev_episode_id)", font=F_SMALL_B, fill=YELLOW)

    if t >= 2.3:
        # Pipeline Step B: Slim inline sticky note injected
        slide = ease_out((t - 2.3) / 0.6)
        iy = int(top + 200 + (1 - slide) * 20)
        draw.rounded_rectangle([rx + 20, iy, rx + col_w - 20, iy + 124], radius=10, fill=blend(CARD, GREEN, 0.10), outline=GREEN, width=2)
        draw.text((rx + 34, iy + 10), "2. 在新对话里只“内联”留一张极薄的历史路标：", font=F_BODY_B, fill=GREEN)
        draw.text((rx + 34, iy + 36), "<tmem_compacted_index timeline='#1 -> #2'>", font=F_MONO_B, fill=CYAN)
        draw.text((rx + 34, iy + 60), "  ep_01: JWT leewaySec 架构规则 | ep_02: jwt_guard 修复", font=F_MONO, fill=TEXT)
        draw.text((rx + 34, iy + 88), "只占 603 tok（相比全保留省 91.6%），却留着全量历史入口！", font=F_SMALL_B, fill=YELLOW)

    if t >= 3.0:
        draw_robot(draw, rx + 78, top + 408, mood="happy", scale=1.05, phase=sec)
        draw.rounded_rectangle([rx + 132, top + 345, rx + col_w - 20, top + 468], radius=10, fill=CARD_ALT, outline=GREEN, width=1)
        draw.text((rx + 148, top + 357), "清醒的 Agent：", font=F_BODY_B, fill=GREEN)
        draw.text((rx + 148, top + 382), "“对话变轻了，完整历史还稳稳接在时间线上！”", font=F_BODY, fill=TEXT)
        draw.text((rx + 148, top + 408), "“需不需要全量找回？我自己看着办！”", font=F_BODY_B, fill=CYAN)
        draw.text((rx + 148, top + 435), "0 Summarization Token  |  细节 0 丢失", font=F_SMALL_B, fill=GREEN)


def render_act2(draw, t, sec):
    """Act 2 (4.2..8.8s): Inline Signpost Silence on Casual Chat + Model-Decided L0->L3 / Full History Recall."""
    # Top sub-strip: Casual chat = 0 intrusion
    draw.rounded_rectangle([28, 78, WIDTH - 28, 152], radius=10, fill=CARD, outline=BORDER, width=1)
    draw.text((46, 88), "场景 A（日常闲聊，不需要旧记忆）：", font=F_SMALL_B, fill=MUTED)
    draw.text((310, 86), '用户：“顺手把登录按钮改成圆角蓝色吧”', font=F_BODY_B, fill=TEXT)
    draw.text((46, 118), "tmem 守门员检查：没有点名旧文件/报错，内联路标也无需展开", font=F_BODY, fill=CYAN)
    draw_pill(draw, 690, 112, "保持静音 · 0 误打扰 (0.0% FP)", F_BODY_B, GREEN, blend(CARD, GREEN, 0.15), GREEN)

    # Bottom big stage: Model decides full / deep recall needed!
    top, h = 166, 486
    draw.rounded_rectangle([28, top, WIDTH - 28, top + h], radius=12, fill=CARD, outline=BLUE, width=2)
    draw.rectangle([28, top, WIDTH - 28, top + 40], fill=CARD_ALT)
    draw.text((46, top + 9), "场景 B：模型自己判断“摘要不够，要查当时到底怎么改的！”——按需 L0 -> L3 / 全量找回", font=F_SUB, fill=BLUE)

    # Left column: Conversation & Model Decision
    draw.rounded_rectangle([46, top + 56, 470, top + 132], radius=10, fill=CARD_ALT, outline=YELLOW, width=1)
    draw.text((62, top + 66), "用户提问：", font=F_SMALL_B, fill=YELLOW)
    draw.text((62, top + 92), "“刚 Phase 1 定的 leewaySec 规则，还有", font=F_BODY, fill=TEXT)
    draw.text((62, top + 110), " 测试中间那个 drift 报错，把原对话查出来！”", font=F_BODY, fill=TEXT)

    draw_robot(draw, 102, top + 215, mood="thinking", scale=1.1, phase=sec)
    draw.rounded_rectangle([156, top + 150, 470, top + 286], radius=10, fill=CARD_DARK, outline=CYAN, width=1)
    draw.text((170, top + 160), "模型自主决策：", font=F_BODY_B, fill=CYAN)
    draw.text((170, top + 186), "1. 瞄一眼内联路标 <tmem_compacted_index>", font=F_SMALL, fill=TEXT)
    draw.text((170, top + 210), "2. 判断：光靠压缩摘要不够！", font=F_SMALL_B, fill=YELLOW)
    draw.text((170, top + 234), "3. 决定：调用 tmem_search + tmem_expand", font=F_SMALL_B, fill=GREEN)
    draw.text((170, top + 258), "   按需翻某页，或沿 #1<-#2 找回全量历史！", font=F_SMALL, fill=CYAN)

    # Timeline capsule box bottom-left
    draw.rounded_rectangle([46, top + 306, 470, top + 462], radius=10, fill=CARD_DARK, outline=PURPLE, width=1)
    draw.text((62, top + 318), "为什么模型能随时找回完整会话历史？", font=F_BODY_B, fill=PURPLE)
    draw.text((62, top + 346), "• 当前上下文里永远保留轻量化「内联路标」", font=F_SMALL, fill=TEXT)
    draw.text((62, top + 372), "• 每次压缩产生的 .tmem/episodes/ 自动链表串联", font=F_SMALL, fill=TEXT)
    draw.text((62, top + 398), "  [ep_01] <── prev_episode_id ── [ep_02]", font=F_MONO_B, fill=CYAN)
    draw.text((62, top + 424), "• 平时不撑爆 Token，需要全量回顾时随时按需拉取", font=F_SMALL_B, fill=GREEN)

    # Right column: Animated L0 -> L1 -> L2 -> L3 Pyramid / Zoom Stack
    rx = 496
    rw = 656
    tiers = [
        (0.4, "L0 目录定位", 'tmem_search("leewaySec drift") -> 命中 ep_01, ep_02', "~6 ms  |  只返回坐标，不污染正文", BLUE),
        (1.2, "L1 阶段梗概", 'tmem_expand(id="ep_01", level="slice")', "还原决策: leewaySec=30s | Turn 目录: #1..#6", CYAN),
        (2.1, "L2 对话/Diff", 'tmem_expand(id="ep_02", level="turn" / "diff")', "找回完整 Turn 对话内容 & jwt_guard.ts 精确补丁", YELLOW),
        (3.0, "L3 全量原文", 'tmem_expand(id="ep_02", level="raw", query="DRIFT")', "从 18KB 截断日志正中间精准切出报错栈原文！", GREEN),
    ]

    cur_y = top + 56
    for idx, (ta, tag, cmd, res, col) in enumerate(tiers):
        active = t >= ta
        highlight = t >= ta and (idx == 3 or t < tiers[idx + 1][0])
        bd = col if highlight else (blend(BORDER, col, 0.5) if active else BORDER)
        bg = blend(CARD_DARK, col, 0.14 if highlight else 0.05) if active else CARD_DARK
        draw.rounded_rectangle([rx, cur_y, rx + rw, cur_y + 88], radius=10, fill=bg, outline=bd, width=2 if highlight else 1)

        badge_fg = BG if active else MUTED
        badge_bg = col if active else CARD_ALT
        draw_pill(draw, rx + 14, cur_y + 12, tag, F_BODY_B, badge_fg, badge_bg, col if active else BORDER, px=10, py=4, r=6)
        draw.text((rx + 150, cur_y + 14), cmd, font=F_MONO_B if active else F_MONO, fill=TEXT if active else DIM)
        draw.text((rx + 150, cur_y + 48), res, font=F_SMALL_B if highlight else F_SMALL, fill=col if active else DIM)

        # Connector arrow down
        if idx < 3:
            ay = cur_y + 90
            acol = col if t >= tiers[idx + 1][0] else BORDER
            draw.line([rx + 64, ay, rx + 64, ay + 12], fill=acol, width=3)
        cur_y += 104


def render_act3(draw, t, sec):
    """Act 3 (8.8..12.6s): L1 Co-Change + L2 Instant Error Rescue | Right: <5ms AST Signature Guard Shield."""
    col_w = 560
    lx, rx = 28, 612
    top, h = 82, 570

    # --- LEFT CARD: Runtime Strong Signal Rescue ---
    draw.rounded_rectangle([lx, top, lx + col_w, top + h], radius=12, fill=CARD, outline=YELLOW, width=2)
    draw.rectangle([lx, top, lx + col_w, top + 40], fill=CARD_ALT)
    draw_lightning(draw, lx + 18, top + 9, 0.9, YELLOW)
    draw.text((lx + 40, top + 9), "运行时“及时雨”：漏改搭档提醒 + 同款报错秒救", font=F_SUB, fill=YELLOW)

    # Step 1: Edit + L1 Co-change
    if t >= 0.2:
        draw.rounded_rectangle([lx + 20, top + 58, lx + col_w - 20, top + 162], radius=10, fill=CARD_DARK, outline=CYAN, width=1)
        draw.text((lx + 34, top + 68), "[1] 模型刚改完 src/auth.ts，钩子毫秒级提醒：", font=F_BODY_B, fill=TEXT)
        draw.text((lx + 34, top + 98), "<tmem_cochange_hint> 以前改 auth.ts 有 92% 概率", font=F_MONO, fill=CYAN)
        draw.text((lx + 34, top + 122), "    要一起改 tests/auth.test.ts！（忽略3次自动闭嘴）", font=F_MONO, fill=MUTED)

    # Step 2: Command fails -> L2 Instant Intercept
    if t >= 1.1:
        draw.rounded_rectangle([lx + 20, top + 180, lx + col_w - 20, top + 365], radius=10, fill=CARD_DARK, outline=RED if t < 2.2 else GREEN, width=2)
        draw.text((lx + 34, top + 190), "[2] 运行测试报错：ERR_TOKEN_DRIFT", font=F_BODY_B, fill=RED)
        if t >= 1.6:
            draw_lightning(draw, lx + 36, top + 219, 0.78, YELLOW)
            draw.text((lx + 54, top + 220), "lookupL2Repair 瞬间命中同源历史修复：", font=F_BODY_B, fill=PURPLE)
            draw.rounded_rectangle([lx + 34, top + 248, lx + col_w - 34, top + 306], radius=6, fill=CARD_ALT, outline=YELLOW, width=1)
            draw.text((lx + 46, top + 258), "上次遇到这个错，给 verify() 加了 clockTolerance: 30", font=F_SMALL_B, fill=YELLOW)
            draw.text((lx + 46, top + 282), "按此修改后重跑 -> 验证直接转绿！", font=F_SMALL, fill=CYAN)
        if t >= 2.3:
            draw_check(draw, lx + 36, top + 324, 15, GREEN)
            draw.text((lx + 60, top + 322), "PASS! 原本 8 步瞎试，现在 5 步搞定 (-37.5% 步数)", font=F_BODY_B, fill=GREEN)

    if t >= 2.6:
        draw.rounded_rectangle([lx + 20, top + 384, lx + col_w - 20, top + 548], radius=10, fill=CARD_ALT, outline=BORDER, width=1)
        draw.text((lx + 36, top + 398), "并且每次都会记账（.tmem/telemetry.jsonl）：", font=F_BODY_B, fill=TEXT)
        draw.text((lx + 36, top + 430), "• 这条报错提醒真帮上忙了吗？ -> 计入 L2 转绿率 (100%)", font=F_SMALL, fill=GREEN)
        draw.text((lx + 36, top + 458), "• 这个搭档提醒总被模型无视吗？ -> 连续 3 次忽略就自动禁言", font=F_SMALL, fill=CYAN)
        draw.text((lx + 36, top + 486), "• 会不会变成永远不删的垃圾？ -> 写前去重合并 + 定时 GC", font=F_SMALL, fill=YELLOW)

    # --- RIGHT CARD: AST Signature Guard Shield ---
    draw.rounded_rectangle([rx, top, rx + col_w, top + h], radius=12, fill=CARD, outline=BLUE, width=2)
    draw.rectangle([rx, top, rx + col_w, top + 40], fill=CARD_ALT)
    draw.text((rx + 20, top + 9), "防“毒记忆”盾牌：代码一重构，老剧本自动降级", font=F_SUB, fill=BLUE)

    # New refactored code card
    draw.rounded_rectangle([rx + 20, top + 58, rx + col_w - 20, top + 150], radius=10, fill=CARD_DARK, outline=CYAN, width=1)
    draw.text((rx + 34, top + 68), "刚才开发者重构了函数参数：", font=F_SMALL_B, fill=MUTED)
    draw.text((rx + 34, top + 94), "旧签名: verifyToken(tok)", font=F_MONO, fill=DIM)
    draw.text((rx + 34, top + 118), "新签名: verifyToken(tok, strictMode)  <-- 参数变了!", font=F_MONO_B, fill=CYAN)

    # Stale memory flying toward Shield & deflecting!
    bounce_t = clamp01((t - 0.8) / 1.4)
    shield_cx = rx + 340
    shield_cy = top + 275
    pulse = abs(math.sin(sec * 7.0)) if 1.1 <= t <= 2.3 else 0.0
    draw_shield(draw, shield_cx, shield_cy, r=42, col=BLUE, pulse=pulse)
    draw.text((shield_cx - 64, shield_cy + 50), "AST 体检 (<5ms)", font=F_SMALL_B, fill=BLUE)

    # Old playbook box
    if t < 1.5:
        pb_x = int(rx + 24 + ease_out(t / 1.3) * 70)
    else:
        pb_x = int(rx + 94 - ease_out((t - 1.5) / 0.7) * 58)
    pb_col = RED if t >= 1.4 else YELLOW
    draw.rounded_rectangle([pb_x, top + 215, pb_x + 215, top + 325], radius=10, fill=CARD_DARK, outline=pb_col, width=2)
    draw.text((pb_x + 12, top + 228), "旧剧本试图注入：", font=F_SMALL_B, fill=pb_col)
    draw.text((pb_x + 12, top + 254), "直接调 verifyToken(tok)", font=F_MONO, fill=TEXT)
    if t >= 1.5:
        draw_pill(draw, pb_x + 12, top + 284, "拦截! signature_match 失败", F_SMALL_B, RED, blend(CARD, RED, 0.18), RED, px=8, py=3)

    if t >= 2.2:
        draw.rounded_rectangle([rx + 20, top + 368, rx + col_w - 20, top + 548], radius=10, fill=blend(CARD, BLUE, 0.12), outline=GREEN, width=2)
        draw.text((rx + 36, top + 382), "自动处理结果：绝不给模型塞过时代码！", font=F_BODY_B, fill=GREEN)
        draw.text((rx + 36, top + 414), "1. 自动把旧剧本从「强指引 L0_FULL」降级为「L1 思路提醒」", font=F_SMALL, fill=TEXT)
        draw.text((rx + 36, top + 442), "2. 屏蔽写死的旧函数调用，只保留“需要校验时钟漂移”思路", font=F_SMALL, fill=CYAN)
        draw.text((rx + 36, top + 470), "3. 支持 Python / JS / TS / Go / Rust / Java / C++", font=F_SMALL, fill=YELLOW)
        draw.text((rx + 36, top + 502), "-> 代码再怎么重构，也不会被陈年记忆带偏！", font=F_BODY_B, fill=GREEN)


def render_act4(draw, t, sec):
    """Act 4 (12.6..16.0s): Verified Scorecard + Install CTA."""
    draw.text((34, 86), "全部本地脚本可复现的实测结果（Node.js >= 22.5 · 零第三方依赖 · 不耗模型总结费）", font=F_TITLE, fill=TEXT)

    cards = [
        (0.1, "压缩后细节找回率", "100.0%", "原生 Compact 基线只有 0.0%\n4/4 场景完美按需还原", GREEN),
        (0.4, "上下文 Token 节省", "-91.6%", "L0 路标仅 603 vs 7,157 tok\n含按需展开仍省 87.0%", CYAN),
        (0.7, "日常对话误打扰率", "0.0%", "普通闲聊 0 误注入\n总结记忆消耗 0 LLM Token", BLUE),
        (1.0, "50 轨迹切片准确率", "93.6% F1", "5 位评委盲测胜率 49:1 (98%)\nDSH 端到端少走 37.5% 步数", PURPLE),
    ]

    cw = 266
    ch = 268
    gx = 28
    for i, (ta, title, big, sub, col) in enumerate(cards):
        cx = 30 + i * (cw + gx)
        cy = 136
        p = ease_out((t - ta) / 0.5)
        if p <= 0:
            continue
        dy = int((1 - p) * 24)
        draw.rounded_rectangle([cx, cy + dy, cx + cw, cy + dy + ch], radius=14, fill=CARD, outline=col, width=2)
        draw.rectangle([cx, cy + dy, cx + cw, cy + dy + 44], fill=CARD_ALT)
        draw.text((cx + 18, cy + dy + 11), title, font=F_SUB, fill=col)
        draw.text((cx + 22, cy + dy + 72), big, font=F_BIG, fill=TEXT)
        line_y = cy + dy + 148
        for line in sub.split("\n"):
            draw.text((cx + 20, line_y), line, font=F_SMALL, fill=MUTED)
            line_y += 26

    # Bottom terminal CTA
    if t >= 1.2:
        draw.rounded_rectangle([30, 440, WIDTH - 30, 646], radius=14, fill=CARD_DARK, outline=GREEN, width=2)
        draw.text((54, 460), "30 秒接入你的 DeepSeek Harness (dsh) 项目：", font=F_SUB, fill=GREEN)

        cmds = [
            ("$ npm install -g dsh-tmem && tmem init", "初始化 .tmem/、挂载 DSH 插件与 git post-commit 钩子", CYAN),
            ("$ dsh plugin add npm:dsh-tmem", "或直接让 dsh 从 npm / github 安装插件", BLUE),
            ("$ /tmem  |  tmem_search  |  tmem_expand", "模型自己按需查历史，随时也能手动存档和翻看", YELLOW),
        ]
        cy = 502
        for cmd, desc, col in cmds:
            draw.text((54, cy), cmd, font=F_MONO_B, fill=col)
            draw.text((540, cy + 1), desc, font=F_SMALL, fill=MUTED)
            cy += 42

        # Cute mascot waving at right
        draw_robot(draw, WIDTH - 94, 540, mood="happy", scale=1.15, phase=sec)


def render_frame(frame_idx):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)
    sec = frame_idx / FPS

    # Act split: 0-4.2, 4.2-8.8, 8.8-12.6, 12.6-16.0
    if sec < 4.2:
        act_idx = 0
        draw_header(draw, sec, act_idx)
        render_act1(draw, sec, sec)
    elif sec < 8.8:
        act_idx = 1
        draw_header(draw, sec, act_idx)
        render_act2(draw, sec - 4.2, sec)
    elif sec < 12.6:
        act_idx = 2
        draw_header(draw, sec, act_idx)
        render_act3(draw, sec - 8.8, sec)
    else:
        act_idx = 3
        draw_header(draw, sec, act_idx)
        render_act4(draw, sec - 12.6, sec)

    draw_footer(draw, sec)
    return img


def main():
    frame_dir = "/tmp/tmem_frames"
    if os.path.exists(frame_dir):
        shutil.rmtree(frame_dir)
    os.makedirs(frame_dir, exist_ok=True)

    print(f"Rendering {TOTAL_FRAMES} frames ({TOTAL_SECONDS}s @ {FPS}fps)...")
    for i in range(TOTAL_FRAMES):
        frame = render_frame(i)
        frame.save(f"{frame_dir}/frame_{i:04d}.png")
        if i % 60 == 0:
            print(f"  Frame {i}/{TOTAL_FRAMES} rendered")

    os.makedirs("docs/assets", exist_ok=True)
    mp4_path = "docs/assets/tmem-demo.mp4"
    gif_path = "docs/assets/tmem-demo.gif"

    print("Encoding MP4 with ffmpeg...")
    subprocess.run([
        "ffmpeg", "-y",
        "-r", str(FPS),
        "-i", f"{frame_dir}/frame_%04d.png",
        "-c:v", "libx264",
        "-profile:v", "high",
        "-level", "4.0",
        "-pix_fmt", "yuv420p",
        "-crf", "18",
        "-movflags", "+faststart",
        mp4_path,
    ], check=True)

    print("Encoding optimized GIF with palettegen...")
    palette_path = "/tmp/tmem_palette.png"
    subprocess.run([
        "ffmpeg", "-y",
        "-i", mp4_path,
        "-vf", "fps=12,scale=960:-1:flags=lanczos,palettegen=stats_mode=diff",
        palette_path,
    ], check=True)

    subprocess.run([
        "ffmpeg", "-y",
        "-i", mp4_path,
        "-i", palette_path,
        "-lavfi", "fps=12,scale=960:-1:flags=lanczos [x]; [x][1:v] paletteuse=dither=bayer:bayer_scale=3",
        gif_path,
    ], check=True)

    shutil.rmtree(frame_dir)
    if os.path.exists(palette_path):
        os.remove(palette_path)

    print("Done!")
    print(f"MP4 generated: {mp4_path} ({os.path.getsize(mp4_path)} bytes)")
    print(f"GIF generated: {gif_path} ({os.path.getsize(gif_path)} bytes)")


if __name__ == "__main__":
    main()
