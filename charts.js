/* Hand-rolled SVG charts. No charting library -- the project has a hard
   zero-dependency rule, and these three chart types are a few hundred lines.

   Everything is drawn into a viewBox and scaled by CSS, so it stays sharp and
   works on a phone. Colours come from the CSS custom properties so the charts
   follow the light/dark theme instead of fighting it. */

const CH = (() => {
  const NS = 'http://www.w3.org/2000/svg';

  function el(name, attrs = {}, text) {
    const e = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs)) {
      if (v !== null && v !== undefined) e.setAttribute(k, v);
    }
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function css(name, fallback) {
    const v = getComputedStyle(document.documentElement)
      .getPropertyValue(name).trim();
    return v || fallback;
  }

  const palette = () => ({
    accent: css('--accent', '#2fbf87'),
    info: css('--info', '#5b9bd5'),
    warn: css('--warn', '#e0a33a'),
    danger: css('--danger', '#e2685f'),
    border: css('--border', '#2b3441'),
    grid: css('--border-soft', '#222a35'),
    text: css('--text-secondary', '#a3b0c2'),
    muted: css('--text-muted', '#6f7d91'),
    surface: css('--surface-1', '#1a2028'),
  });

  const fmtMoney = (v) => {
    const a = Math.abs(v);
    if (a >= 1e7) return (v / 1e7).toFixed(2) + ' Cr';
    if (a >= 1e5) return (v / 1e5).toFixed(1) + ' L';
    if (a >= 1e3) return (v / 1e3).toFixed(0) + 'k';
    return v.toFixed(0);
  };

  /* ------------------------------------------------------------------
     The main screen: expected profit vs bid, with win probability on a
     second axis, the optimum marked, a bootstrap confidence band around
     the optimal bid, and the breakeven line.
     ------------------------------------------------------------------ */
  function profitCurve(container, data, opts = {}) {
    const P = palette();
    const W = 760, H = 330;
    const m = { t: 18, r: 54, b: 44, l: 66 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;

    const pts = data.curve;
    if (!pts || !pts.length) { container.textContent = 'No curve to draw.'; return; }

    const xs = pts.map(p => p.bid);
    const xMin = Math.min(...xs), xMax = Math.max(...xs);
    const profits = pts.map(p => p.expected_profit);
    let yMin = Math.min(0, Math.min(...profits));
    let yMax = Math.max(...profits);
    if (yMax <= yMin) yMax = yMin + 1;
    const pad = (yMax - yMin) * 0.12;
    yMin -= pad; yMax += pad;

    const X = v => m.l + ((v - xMin) / (xMax - xMin)) * iw;
    const Y = v => m.t + ih - ((v - yMin) / (yMax - yMin)) * ih;
    const Yp = p => m.t + ih - p * ih;             // win probability 0..1

    const svg = el('svg', {
      class: 'chart', viewBox: `0 0 ${W} ${H}`,
      role: 'img',
      'aria-label': 'Expected profit and win probability as a function of bid price',
    });

    // --- grid + axes
    const yTicks = 5;
    for (let i = 0; i <= yTicks; i++) {
      const v = yMin + (yMax - yMin) * (i / yTicks);
      const y = Y(v);
      svg.appendChild(el('line', {
        x1: m.l, x2: m.l + iw, y1: y, y2: y,
        stroke: v === 0 ? P.border : P.grid,
        'stroke-width': v === 0 ? 1 : 0.5,
      }));
      svg.appendChild(el('text', {
        x: m.l - 8, y: y + 3.5, 'text-anchor': 'end',
        fill: P.muted, 'font-size': 10.5, 'font-family': 'ui-monospace, monospace',
      }, fmtMoney(v)));
    }
    for (let i = 0; i <= 4; i++) {
      const p = i / 4;
      svg.appendChild(el('text', {
        x: m.l + iw + 8, y: Yp(p) + 3.5, 'text-anchor': 'start',
        fill: P.muted, 'font-size': 10.5, 'font-family': 'ui-monospace, monospace',
      }, Math.round(p * 100) + '%'));
    }
    const xTicks = 6;
    for (let i = 0; i <= xTicks; i++) {
      const v = xMin + (xMax - xMin) * (i / xTicks);
      svg.appendChild(el('line', {
        x1: X(v), x2: X(v), y1: m.t, y2: m.t + ih, stroke: P.grid, 'stroke-width': 0.5,
      }));
      svg.appendChild(el('text', {
        x: X(v), y: m.t + ih + 16, 'text-anchor': 'middle',
        fill: P.muted, 'font-size': 10.5, 'font-family': 'ui-monospace, monospace',
      }, fmtMoney(v)));
    }

    svg.appendChild(el('text', {
      x: m.l + iw / 2, y: H - 8, 'text-anchor': 'middle',
      fill: P.text, 'font-size': 11.5,
    }, 'Your bid (Rs)'));
    svg.appendChild(el('text', {
      x: 13, y: m.t + ih / 2, 'text-anchor': 'middle', fill: P.text,
      'font-size': 11.5, transform: `rotate(-90 13 ${m.t + ih / 2})`,
    }, 'Expected profit'));
    svg.appendChild(el('text', {
      x: W - 11, y: m.t + ih / 2, 'text-anchor': 'middle', fill: P.text,
      'font-size': 11.5, transform: `rotate(90 ${W - 11} ${m.t + ih / 2})`,
    }, 'P(win)'));

    // --- bootstrap confidence band around the optimal bid
    const ciBid = data.recommendation && data.recommendation.bid_ci;
    if (ciBid && ciBid[0] != null) {
      const x0 = X(Math.max(ciBid[0], xMin)), x1 = X(Math.min(ciBid[1], xMax));
      svg.appendChild(el('rect', {
        x: Math.min(x0, x1), y: m.t, width: Math.abs(x1 - x0), height: ih,
        fill: P.accent, opacity: 0.11,
      }));
    }

    // --- win probability curve (secondary axis)
    let wp = '';
    pts.forEach((p, i) => { wp += (i ? 'L' : 'M') + X(p.bid) + ' ' + Yp(p.win_prob); });
    svg.appendChild(el('path', {
      d: wp, fill: 'none', stroke: P.info, 'stroke-width': 1.6,
      'stroke-dasharray': '4 3', opacity: 0.9,
    }));

    // --- expected profit curve, filled under the positive part
    let area = `M${X(pts[0].bid)} ${Y(Math.max(0, yMin))}`;
    pts.forEach(p => { area += `L${X(p.bid)} ${Y(p.expected_profit)}`; });
    area += `L${X(pts[pts.length - 1].bid)} ${Y(Math.max(0, yMin))}Z`;
    svg.appendChild(el('path', { d: area, fill: P.accent, opacity: 0.1 }));

    let line = '';
    pts.forEach((p, i) => { line += (i ? 'L' : 'M') + X(p.bid) + ' ' + Y(p.expected_profit); });
    svg.appendChild(el('path', {
      d: line, fill: 'none', stroke: P.accent, 'stroke-width': 2.2,
      'stroke-linejoin': 'round',
    }));

    // --- breakeven (your cost)
    const cost = data.breakeven && data.breakeven.bid;
    if (cost != null && cost >= xMin && cost <= xMax) {
      svg.appendChild(el('line', {
        x1: X(cost), x2: X(cost), y1: m.t, y2: m.t + ih,
        stroke: P.danger, 'stroke-width': 1.2, 'stroke-dasharray': '3 3',
      }));
      svg.appendChild(el('text', {
        x: X(cost) + 5, y: m.t + 12, fill: P.danger, 'font-size': 10.5,
      }, 'your cost'));
    }

    // --- the optimum
    const rec = data.recommendation;
    if (rec && rec.bid != null) {
      const ox = X(rec.bid), oy = Y(rec.expected_profit);
      svg.appendChild(el('line', {
        x1: ox, x2: ox, y1: oy, y2: m.t + ih,
        stroke: P.accent, 'stroke-width': 1.2, 'stroke-dasharray': '2 3',
      }));
      svg.appendChild(el('circle', {
        cx: ox, cy: oy, r: 5, fill: P.accent, stroke: P.surface, 'stroke-width': 2,
      }));
      const labelLeft = ox > m.l + iw * 0.62;
      svg.appendChild(el('text', {
        x: labelLeft ? ox - 10 : ox + 10, y: oy - 11,
        'text-anchor': labelLeft ? 'end' : 'start',
        fill: P.accent, 'font-size': 11.5, 'font-weight': 600,
      }, 'Rs ' + fmtMoney(rec.bid)));
    }

    // --- your own bid, if one is being evaluated
    if (opts.yourBid != null && opts.yourBid >= xMin && opts.yourBid <= xMax) {
      const bx = X(opts.yourBid);
      const near = pts.reduce((a, b) =>
        Math.abs(b.bid - opts.yourBid) < Math.abs(a.bid - opts.yourBid) ? b : a);
      svg.appendChild(el('line', {
        x1: bx, x2: bx, y1: m.t, y2: m.t + ih, stroke: P.warn, 'stroke-width': 1.4,
      }));
      svg.appendChild(el('circle', {
        cx: bx, cy: Y(near.expected_profit), r: 4.5, fill: P.warn,
        stroke: P.surface, 'stroke-width': 2,
      }));
      svg.appendChild(el('text', {
        x: bx + 6, y: m.t + 26, fill: P.warn, 'font-size': 10.5,
      }, 'your bid'));
    }

    container.innerHTML = '';
    container.appendChild(svg);

    const legend = document.createElement('div');
    legend.className = 'legend';
    legend.innerHTML =
      `<span><i class="swatch" style="background:${P.accent}"></i> Expected profit</span>` +
      `<span><i class="swatch" style="background:${P.info}"></i> P(win)</span>` +
      `<span><i class="swatch" style="background:${P.danger}"></i> Your cost</span>` +
      `<span><i class="swatch" style="background:${P.accent};opacity:.35"></i> 90% interval on the optimal bid</span>`;
    container.appendChild(legend);
  }

  /* ------------------------------------------------------------------
     Histogram of historical bid ratios, with markers. Shows the bidder
     the actual distribution their recommendation came out of.
     ------------------------------------------------------------------ */
  function histogram(container, values, markers = [], opts = {}) {
    const P = palette();
    const W = 760, H = 190;
    const m = { t: 14, r: 14, b: 38, l: 44 };
    const iw = W - m.l - m.r, ih = H - m.t - m.b;
    if (!values || values.length < 2) {
      container.innerHTML = '<div class="empty">Not enough history to plot.</div>';
      return;
    }

    const lo = opts.min != null ? opts.min : Math.min(...values);
    const hi = opts.max != null ? opts.max : Math.max(...values);
    const bins = opts.bins || 26;
    const counts = new Array(bins).fill(0);
    values.forEach(v => {
      let i = Math.floor(((v - lo) / (hi - lo)) * bins);
      i = Math.max(0, Math.min(bins - 1, i));
      counts[i]++;
    });
    const cMax = Math.max(...counts) || 1;

    const X = v => m.l + ((v - lo) / (hi - lo)) * iw;
    const svg = el('svg', {
      class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img',
      'aria-label': 'Distribution of historical bid ratios in this bucket',
    });

    counts.forEach((c, i) => {
      const bw = iw / bins;
      const h = (c / cMax) * ih;
      svg.appendChild(el('rect', {
        x: m.l + i * bw + 0.7, y: m.t + ih - h, width: Math.max(1, bw - 1.4),
        height: h, fill: P.info, opacity: 0.55, rx: 1.5,
      }));
    });

    svg.appendChild(el('line', {
      x1: m.l, x2: m.l + iw, y1: m.t + ih, y2: m.t + ih,
      stroke: P.border, 'stroke-width': 1,
    }));
    for (let i = 0; i <= 5; i++) {
      const v = lo + (hi - lo) * (i / 5);
      svg.appendChild(el('text', {
        x: X(v), y: m.t + ih + 15, 'text-anchor': 'middle', fill: P.muted,
        'font-size': 10.5, 'font-family': 'ui-monospace, monospace',
      }, v.toFixed(2)));
    }
    svg.appendChild(el('text', {
      x: m.l + iw / 2, y: H - 6, 'text-anchor': 'middle', fill: P.text,
      'font-size': 11.5,
    }, opts.xLabel || 'Bid / estimated value'));
    svg.appendChild(el('text', {
      x: m.l - 8, y: m.t + 9, 'text-anchor': 'end', fill: P.muted,
      'font-size': 10.5, 'font-family': 'ui-monospace, monospace',
    }, String(cMax)));

    markers.forEach(mk => {
      if (mk.value == null || mk.value < lo || mk.value > hi) return;
      const x = X(mk.value);
      svg.appendChild(el('line', {
        x1: x, x2: x, y1: m.t, y2: m.t + ih, stroke: mk.color || P.accent,
        'stroke-width': 1.6, 'stroke-dasharray': mk.dash || null,
      }));
      svg.appendChild(el('text', {
        x: x + 4, y: m.t + 11, fill: mk.color || P.accent, 'font-size': 10.5,
      }, mk.label || ''));
    });

    container.innerHTML = '';
    container.appendChild(svg);
  }

  /* ------------------------------------------------------------------
     Simple horizontal bars, used for competitor hit rates and screens.
     ------------------------------------------------------------------ */
  function bars(container, rows, opts = {}) {
    const P = palette();
    if (!rows || !rows.length) {
      container.innerHTML = '<div class="empty">Nothing to show.</div>';
      return;
    }
    const max = Math.max(...rows.map(r => r.value)) || 1;
    const frag = document.createElement('div');
    rows.forEach(r => {
      const line = document.createElement('div');
      line.style.cssText = 'display:grid;grid-template-columns:1fr 84px;gap:10px;'
        + 'align-items:center;padding:6px 0;';
      const left = document.createElement('div');
      left.innerHTML =
        `<div style="font-size:12.5px;margin-bottom:4px">${r.label}</div>`
        + `<div class="bar"><span style="width:${(r.value / max) * 100}%;`
        + `background:${r.color || P.accent}"></span></div>`;
      const right = document.createElement('div');
      right.className = 'num sec';
      right.textContent = r.display != null ? r.display : r.value.toFixed(2);
      line.appendChild(left); line.appendChild(right);
      frag.appendChild(line);
    });
    container.innerHTML = '';
    container.appendChild(frag);
    void opts;
  }

  return { profitCurve, histogram, bars, fmtMoney };
})();
