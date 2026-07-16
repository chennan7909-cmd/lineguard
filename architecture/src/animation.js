(function () {
  const canvas = document.getElementById("scene");
  const ctx = canvas.getContext("2d");
  const W = canvas.width;
  const H = canvas.height;
  const DURATION = 21;

  const colors = {
    bg0: "#071017",
    bg1: "#101a22",
    ink: "#edf7f4",
    muted: "#b1c0c5",
    line: "#3f5863",
    teal: "#20d6b5",
    mint: "#8df5d4",
    amber: "#ffbf5a",
    red: "#ff6b6b",
    blue: "#72a7ff",
    violet: "#b48cff",
    panel: "#10202a",
    panel2: "#142a35",
    pass: "#78ffd2"
  };

  const nodeW = 236;
  const nodeH = 138;
  const baseY = 466;
  const gap = 16;
  const startX = Math.round((W - (nodeW * 7 + gap * 6)) / 2);

  const nodes = [
    { label: "TxLINE", sub: "Odds + Scores", state: "STREAMING", c: colors.blue },
    { label: "Data Guard", sub: "5+1 Integrity Checks", state: "PASS", c: colors.teal },
    { label: "Signal", sub: "Move Attribution", state: "SHARP", c: colors.amber },
    { label: "Hedge", sub: "3-Way P/L Lock", state: "LOCK", c: colors.violet },
    { label: "Execution", sub: "Fill Lifecycle", state: "FILLED", c: colors.red },
    { label: "Reconcile", sub: "Residual Exposure", state: "COMPLETE", c: colors.mint },
    { label: "Solana Audit", sub: "Devnet Record", state: "VERIFIED", c: colors.teal }
  ].map((node, i) => ({ ...node, x: startX + i * (nodeW + gap), y: baseY, w: nodeW, h: nodeH }));

  function clamp(v, a = 0, b = 1) {
    return Math.max(a, Math.min(b, v));
  }

  function smooth(v) {
    v = clamp(v);
    return v * v * (3 - 2 * v);
  }

  function easeInOut(v) {
    return 0.5 - Math.cos(clamp(v) * Math.PI) / 2;
  }

  function segment(t, start, end) {
    return smooth((t - start) / (end - start));
  }

  function roundedRect(x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function text(txt, x, y, size, color, weight = 500, align = "left") {
    ctx.fillStyle = color;
    ctx.font = `${weight} ${size}px Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif`;
    ctx.textAlign = align;
    ctx.textBaseline = "middle";
    ctx.fillText(txt, x, y);
  }

  function drawBackground(t) {
    const g = ctx.createLinearGradient(0, 0, W, H);
    g.addColorStop(0, colors.bg0);
    g.addColorStop(0.58, colors.bg1);
    g.addColorStop(1, "#182226");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, H);

    ctx.save();
    ctx.globalAlpha = 0.12;
    ctx.strokeStyle = "#5b7580";
    ctx.lineWidth = 1;
    const offset = (t * 12) % 80;
    for (let x = -80 + offset; x < W + 80; x += 80) {
      ctx.beginPath();
      ctx.moveTo(x, 0);
      ctx.lineTo(x - 350, H);
      ctx.stroke();
    }
    for (let y = -80; y < H + 80; y += 80) {
      ctx.beginPath();
      ctx.moveTo(0, y + offset);
      ctx.lineTo(W, y + offset - 250);
      ctx.stroke();
    }
    ctx.restore();
  }

  function drawTitle(t) {
    const a = segment(t, 0.2, 1.3);
    ctx.save();
    ctx.globalAlpha = a;
    text("LineGuard", 128, 112, 66, colors.ink, 760);
    text("Autonomous in-play risk desk", 130, 172, 30, colors.muted, 500);
    ctx.fillStyle = colors.teal;
    ctx.fillRect(130, 214, 250 * segment(t, 0.8, 1.8), 4);
    ctx.restore();
  }

  function nodeStart(index) {
    return 1.35 + index * 1.55;
  }

  function drawNode(node, t, index) {
    const appear = segment(t, 0.75 + index * 0.22, 1.25 + index * 0.22);
    const activeStart = nodeStart(index);
    const active = segment(t, activeStart, activeStart + 0.5) * (1 - segment(t, activeStart + 1.15, activeStart + 1.85));
    const complete = segment(t, activeStart + 0.85, activeStart + 1.35);
    const yLift = (1 - appear) * 20;

    ctx.save();
    ctx.globalAlpha = appear;
    ctx.shadowColor = node.c;
    ctx.shadowBlur = 14 + active * 34;
    ctx.fillStyle = colors.panel;
    roundedRect(node.x, node.y + yLift, node.w, node.h, 8);
    ctx.fill();
    ctx.shadowBlur = 0;
    ctx.lineWidth = 2 + active * 3;
    ctx.strokeStyle = node.c;
    ctx.stroke();

    ctx.fillStyle = node.c;
    ctx.beginPath();
    ctx.arc(node.x + 24, node.y + 32 + yLift, 8 + active * 4, 0, Math.PI * 2);
    ctx.fill();

    text(node.label, node.x + 43, node.y + 34 + yLift, 31, colors.ink, 760);
    text(node.sub, node.x + 24, node.y + 77 + yLift, 24, colors.muted, 560);

    const stateA = Math.max(complete, index === 0 ? segment(t, 1.1, 1.7) : 0);
    ctx.globalAlpha = appear * stateA;
    ctx.fillStyle = "#081319";
    roundedRect(node.x + 24, node.y + 98 + yLift, node.w - 48, 28, 6);
    ctx.fill();
    ctx.strokeStyle = node.c;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    text(node.state, node.x + node.w / 2, node.y + 113 + yLift, 17, node.c, 760, "center");
    ctx.restore();
  }

  function drawArrow(x1, y1, x2, y2, progress, color) {
    const dx = x2 - x1;
    const dy = y2 - y1;
    const ex = x1 + dx * progress;
    const ey = y1 + dy * progress;
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = 4;
    ctx.lineCap = "round";
    ctx.globalAlpha = clamp(progress * 1.4);
    ctx.beginPath();
    ctx.moveTo(x1, y1);
    ctx.lineTo(ex, ey);
    ctx.stroke();

    if (progress > 0.92) {
      const angle = Math.atan2(dy, dx);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.moveTo(x2, y2);
      ctx.lineTo(x2 - 15 * Math.cos(angle - 0.45), y2 - 15 * Math.sin(angle - 0.45));
      ctx.lineTo(x2 - 15 * Math.cos(angle + 0.45), y2 - 15 * Math.sin(angle + 0.45));
      ctx.closePath();
      ctx.fill();
    }
    ctx.restore();
  }

  function drawConnections(t) {
    for (let i = 0; i < nodes.length - 1; i += 1) {
      const a = nodes[i];
      const b = nodes[i + 1];
      const p = segment(t, nodeStart(i) + 0.75, nodeStart(i + 1) + 0.15);
      drawArrow(a.x + a.w, a.y + a.h / 2, b.x, b.y + b.h / 2, p, i === 1 ? colors.teal : colors.line);
    }
  }

  function drawPackets(t) {
    for (let i = 0; i < nodes.length - 1; i += 1) {
      const a = nodes[i];
      const b = nodes[i + 1];
      const p = easeInOut((t - (nodeStart(i) + 0.7)) / 1.05);
      if (p <= 0 || p >= 1) continue;
      const x = a.x + a.w + (b.x - a.x - a.w) * p;
      const y = a.y + a.h / 2;
      ctx.save();
      ctx.globalAlpha = 0.95;
      ctx.fillStyle = i === 2 ? colors.amber : colors.teal;
      roundedRect(x - 18, y - 8, 36, 16, 6);
      ctx.fill();
      ctx.restore();
    }
  }

  function drawGuardChecks(t) {
    const guard = nodes[1];
    const reveal = segment(t, 3.0, 3.45);
    const collapse = segment(t, 7.25, 7.95);
    const a = reveal * (1 - collapse);
    if (a <= 0.01) return;

    const h = 238 * (1 - collapse) + 16 * collapse;
    const x = guard.x - 88;
    const y = guard.y - 280;
    const w = guard.w + 250;
    ctx.save();
    ctx.globalAlpha = a;
    ctx.fillStyle = colors.panel2;
    roundedRect(x, y, w, h, 8);
    ctx.fill();
    ctx.strokeStyle = colors.teal;
    ctx.lineWidth = 2;
    ctx.stroke();

    if (collapse < 0.45) {
      text("5+1 INTEGRITY CHECKS", x + 22, y + 29, 21, colors.ink, 760);
      text("CRYPTOGRAPHIC PROVENANCE", x + 22, y + 56, 13, colors.teal, 760);
      const rows = [
        ["G1", "Freshness", colors.pass],
        ["G2", "Demargin consistency", colors.pass],
        ["G3", "Price consistency", colors.pass],
        ["G4", "Range sanity", colors.pass],
        ["G5", "Timestamp monotonicity", colors.pass],
        ["G6", "Merkle proof → on-chain validate_odds", colors.teal]
      ];
      rows.forEach(([code, label, accent], i) => {
        const rowA = segment(t, 3.45 + i * 0.55, 3.9 + i * 0.55);
        const rowY = y + 86 + i * 24;
        ctx.globalAlpha = a * rowA;
        ctx.fillStyle = accent;
        ctx.beginPath();
        ctx.arc(x + 25, rowY, i === 5 ? 6 : 5.5, 0, Math.PI * 2);
        ctx.fill();
        text(code, x + 43, rowY, 15, accent, 780);
        text(label, x + 80, rowY, i === 5 ? 14 : 15, colors.muted, 650);
        text("PASS", x + w - 18, rowY, 15, i === 5 ? colors.teal : colors.pass, 780, "right");
      });
    }
    ctx.restore();
  }

  function drawHedgeEqualization(t) {
    const hedge = nodes[3];
    const a = segment(t, 7.4, 8.0) * (1 - segment(t, 12.4, 13.2));
    if (a <= 0.01) return;

    const x = hedge.x - 84;
    const y = hedge.y + hedge.h + 44;
    const w = 396;
    const h = 188;
    const align = segment(t, 9.0, 10.9);
    const levels = [
      { label: "HOME", from: 122, to: 82, c: colors.blue },
      { label: "DRAW", from: 64, to: 82, c: colors.amber },
      { label: "AWAY", from: 106, to: 82, c: colors.red }
    ];

    ctx.save();
    ctx.globalAlpha = a;
    ctx.fillStyle = "#101d24";
    roundedRect(x, y, w, h, 8);
    ctx.fill();
    ctx.strokeStyle = colors.violet;
    ctx.lineWidth = 2;
    ctx.stroke();
    text("Hedge equalization", x + 24, y + 31, 22, colors.ink, 760);
    text("h_j = T * p_j", x + w - 24, y + 31, 18, colors.muted, 600, "right");

    ctx.strokeStyle = "#324a55";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.moveTo(x + 92, y + 126);
    ctx.lineTo(x + w - 28, y + 126);
    ctx.stroke();

    levels.forEach((item, i) => {
      const rowY = y + 69 + i * 34;
      const value = item.from + (item.to - item.from) * align;
      const barW = 54 + value * 1.65;
      text(item.label, x + 24, rowY, 17, colors.muted, 760);
      ctx.fillStyle = "#0a151c";
      roundedRect(x + 92, rowY - 9, 224, 18, 5);
      ctx.fill();
      ctx.fillStyle = item.c;
      roundedRect(x + 92, rowY - 9, barW, 18, 5);
      ctx.fill();
    });

    ctx.globalAlpha = a * segment(t, 10.7, 11.35);
    text("TERMINAL P/L EQUALIZED", x + w / 2, y + 162, 21, colors.pass, 760, "center");
    ctx.restore();
  }

  function drawClosing(t) {
    const a = segment(t, 17.55, 18.2);
    ctx.save();
    ctx.globalAlpha = a;
    text("EVERY DECISION BECOMES A CHECKABLE CLAIM.", 126, 884, 34, colors.ink, 760);
    ctx.restore();
  }

  function drawTimeline(t) {
    ctx.save();
    ctx.globalAlpha = 0.72;
    ctx.fillStyle = "#223540";
    roundedRect(130, 1012, 300, 8, 4);
    ctx.fill();
    ctx.fillStyle = colors.teal;
    roundedRect(130, 1012, 300 * clamp(t / DURATION), 8, 4);
    ctx.fill();
    ctx.restore();
  }

  function renderFrame(t) {
    drawBackground(t);
    drawTitle(t);
    drawConnections(t);
    drawPackets(t);
    nodes.forEach((node, i) => drawNode(node, t, i));
    drawGuardChecks(t);
    drawHedgeEqualization(t);
    drawClosing(t);
    drawTimeline(t);
  }

  window.renderFrame = renderFrame;
  window.animationDuration = DURATION;

  let start = performance.now();
  function loop(now) {
    const t = ((now - start) / 1000) % DURATION;
    if (!window.__pauseAnimation) {
      renderFrame(t);
    }
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
})();
