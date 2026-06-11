'use strict';

// ── Constants ──────────────────────────────────────────────────────────────

const BASE_LTV_MEAN = 68;          // % national average LTV (DNB 2023)
const LTV_STD      = 18;           // % standard deviation
const MORTGAGE_PENETRATION = 0.57; // share of households with mortgage
const AVG_HH_SIZE  = 2.2;

const SCENARIOS = {
  baseline: {
    id: 'baseline',
    label: 'Baseline (2024)',
    color: '#94a3b8', colorDark: '#64748b',
    floodFactor: 0, foundFactor: 0, droughtFactor: 0, heatFactor: 0, pluvialFactor: 0,
    desc: 'Current market conditions. No climate-adjusted discount applied to property values. A small residual share of mortgages (~3%) is already at LTV > 100% — legacy of the post-2013 housing-market correction.',
  },
  moderate: {
    id: 'moderate',
    label: 'Moderate · ~2 °C · 2050',
    color: '#fbbf24', colorDark: '#d97706',
    floodFactor: 0.06, foundFactor: 0.10, droughtFactor: 0.04, heatFactor: 0.03, pluvialFactor: 0.04,
    desc: 'Flood ≈ 6%; foundation ≈ 10%; drought ≈ 4%; heat stress ≈ 3%; pluvial flooding ≈ 4%. Partial market internalisation of physical risk (Bernstein et al. 2019; Mooij et al. 2022).',
  },
  severe: {
    id: 'severe',
    label: 'Severe · ~3 °C · 2070',
    color: '#fb923c', colorDark: '#ea580c',
    floodFactor: 0.14, foundFactor: 0.22, droughtFactor: 0.09, heatFactor: 0.07, pluvialFactor: 0.09,
    desc: 'Flood ≈ 14%; foundation ≈ 22%; drought ≈ 9%; heat stress ≈ 7% (urban lethal heat events); pluvial ≈ 9% (2021-type rainfall frequency × 3). TNO; DNB Climate Stress Test 2021.',
  },
  extreme: {
    id: 'extreme',
    label: 'Extreme · ~4 °C · 2080',
    color: '#f87171', colorDark: '#dc2626',
    floodFactor: 0.25, foundFactor: 0.38, droughtFactor: 0.16, heatFactor: 0.12, pluvialFactor: 0.15,
    desc: 'Tail-risk scenario. Flood ≈ 25%; foundation ≈ 38%; drought ≈ 16%; heat ≈ 12%; pluvial ≈ 15%. Compounded hazards, infrastructure failure, and stranded-asset dynamics in most exposed areas.',
  },
};

// LTV axis: 30% to 150% in steps of 2%
const LTV_AXIS = [];
for (let v = 30; v <= 150; v += 2) LTV_AXIS.push(v);

// ── Application state ──────────────────────────────────────────────────────

let gemeenteData        = {};   // { 'GM0307': { Naam, Provincie, Population, _woz_value, ... } }
let map                 = null;
let ltvChart            = null;
let activeScenario      = 'baseline';
let rawBuildingFeatures = [];   // last fetched building features (unannotated)
let buildingFetchCtrl   = null; // AbortController for in-flight PDOK requests

// Pre-loaded city building data.
// Amsterdam is loaded eagerly (fly-to demo target). Others load lazily when the
// viewport comes within ~0.5° of the city center.
const preloadedCities = {
  amsterdam: { bbox: [4.856, 52.346, 4.950, 52.402], name: 'Amsterdam',  features: null, loading: false },
  rotterdam: { bbox: [4.430, 51.888, 4.540, 51.948], name: 'Rotterdam',  features: null, loading: false },
  den_haag:  { bbox: [4.265, 52.053, 4.360, 52.105], name: 'Den Haag',   features: null, loading: false },
  utrecht:   { bbox: [5.070, 52.068, 5.155, 52.115], name: 'Utrecht',    features: null, loading: false },
  eindhoven: { bbox: [5.420, 51.415, 5.510, 51.475], name: 'Eindhoven',  features: null, loading: false },
};

async function loadCityBuildings(id) {
  const city = preloadedCities[id];
  if (!city || city.features !== null || city.loading) return;
  city.loading = true;
  try {
    const resp = await fetch(`data/buildings/${id}.geojson`);
    if (!resp.ok) return;
    const data = await resp.json();
    city.features = data.features || [];
  } catch (_) { /* fall through to PDOK */ } finally {
    city.loading = false;
  }
}

function triggerLazyLoads(vW, vS, vE, vN) {
  for (const [id, city] of Object.entries(preloadedCities)) {
    if (city.features !== null || city.loading) continue;
    const [cW, cS, cE, cN] = city.bbox;
    // Start loading if viewport is within 0.3° of the city bbox
    const near = vW < cE + 0.3 && vE > cW - 0.3 && vS < cN + 0.3 && vN > cS - 0.3;
    if (near) loadCityBuildings(id);
  }
}

// ── Math helpers ───────────────────────────────────────────────────────────

function normalCDF(z) {
  // Abramowitz & Stegun approximation, max error 7.5e-8
  const p  = 0.2316419;
  const b  = [0.319381530, -0.356563782, 1.781477937, -1.821255978, 1.330274429];
  const t  = 1 / (1 + p * Math.abs(z));
  let poly = b[4];
  for (let i = 3; i >= 0; i--) poly = poly * t + b[i];
  poly *= t;
  const pdf = Math.exp(-0.5 * z * z) / Math.sqrt(2 * Math.PI);
  const cdf = 1 - pdf * poly;
  return z >= 0 ? cdf : 1 - cdf;
}

function pctAboveLTV(threshold, mean, std) {
  return 1 - normalCDF((threshold - mean) / std);
}

function gaussianPDF(x, mean, std) {
  return Math.exp(-0.5 * ((x - mean) / std) ** 2) / (std * Math.sqrt(2 * Math.PI));
}

// ── Data enrichment ────────────────────────────────────────────────────────

function computeRiskScores(riskData) {
  const provLookup = {};
  for (const [prov, scores] of Object.entries(riskData.province_baseline || {})) {
    provLookup[prov] = scores;
  }
  const overrides = riskData.gemeente_overrides || {};

  let wozMin = Infinity, wozMax = -Infinity;
  for (const gm of Object.values(gemeenteData)) {
    const w = gm._woz_value || 0;
    if (w < wozMin) wozMin = w;
    if (w > wozMax) wozMax = w;
  }
  const wozRange = wozMax - wozMin || 1;

  for (const [code, gm] of Object.entries(gemeenteData)) {
    const prov    = provLookup[gm.Provincie] || { flood: 0.40, foundation: 0.30, drought: 0.22 };
    const ov      = overrides[code] || {};                  // overrides keyed by GM code

    gm._flood_risk      = Math.min(1, Math.max(0, ov.flood      ?? prov.flood      ?? 0.40));
    gm._foundation_risk = Math.min(1, Math.max(0, ov.foundation ?? prov.foundation ?? 0.30));
    gm._drought_risk    = Math.min(1, Math.max(0, ov.drought    ?? prov.drought    ?? 0.22));
    gm._heat_risk           = Math.min(1, Math.max(0, ov.heat       ?? prov.heat       ?? 0.30));
    gm._pluvial_risk        = Math.min(1, Math.max(0, ov.pluvial    ?? prov.pluvial    ?? 0.35));
    gm._elev_m              = ov.flood_risk_ahn_m   ?? null;
    gm._mortgage_penetration = ov.mortgage_penetration ?? null;   // CBS owner-occ × 0.68
    gm._owner_occupied_pct  = ov.owner_occupied_pct  ?? null;
    gm._base_ltv            = ov.base_ltv            ?? BASE_LTV_MEAN; // CBS/DNB per-muni LTV
    gm._woz_norm        = ((gm._woz_value || 0) - wozMin) / wozRange;
  }
}

// ── Scenario computation ───────────────────────────────────────────────────

function municipalDiscount(gm, scenario) {
  const d = scenario.floodFactor   * gm._flood_risk
          + scenario.foundFactor   * gm._foundation_risk
          + scenario.droughtFactor * (gm._drought_risk  || 0)
          + scenario.heatFactor    * (gm._heat_risk     || 0)
          + scenario.pluvialFactor * (gm._pluvial_risk  || 0);
  return Math.min(d, 0.60); // cap at 60%
}

// Precompute _uw_{scenarioId} (% mortgages underwater) on each gm for all scenarios.
// Called once after data loads. Map uses these values via GeoJSON properties.
function precomputeAllScenarios() {
  for (const scenario of Object.values(SCENARIOS)) {
    for (const gm of Object.values(gemeenteData)) {
      const d      = municipalDiscount(gm, scenario);
      const factor = 1 / (1 - d);
      const mu     = (gm._base_ltv || BASE_LTV_MEAN) * factor;
      const sigma  = LTV_STD       * factor;
      gm[`_uw_${scenario.id}`] = Math.round(pctAboveLTV(100, mu, sigma) * 1000) / 10; // % to 1dp
    }
  }
}

function computeScenarioStats(scenarioId) {
  const scenario = SCENARIOS[scenarioId];

  let totalMortgages   = 0;
  let atRiskMortgages  = 0;
  let rawTotalExp      = 0; // in €k
  let rawAtRiskExp     = 0;
  let ltvShiftWeighted = 0;
  let highRiskMunis    = 0;

  for (const gm of Object.values(gemeenteData)) {
    if (!gm.Population) continue;

    const mortgaged  = (gm.Population / AVG_HH_SIZE) * MORTGAGE_PENETRATION;
    const woz        = gm._woz_value || 250;
    const muniLTV    = gm._base_ltv  || BASE_LTV_MEAN;
    const balance    = woz * muniLTV / 100;               // avg mortgage balance in €k
    const exposure   = mortgaged * balance;               // €k

    const d          = municipalDiscount(gm, scenario);
    const factor     = 1 / (1 - d);
    const mu         = muniLTV * factor;
    const sigma      = LTV_STD * factor;
    const puw        = pctAboveLTV(100, mu, sigma);

    totalMortgages   += mortgaged;
    atRiskMortgages  += mortgaged * puw;
    rawTotalExp      += exposure;
    rawAtRiskExp     += exposure * puw;
    ltvShiftWeighted += mortgaged * (mu - muniLTV);
    if (puw * 100 > 5) highRiskMunis++;
  }

  // Express at-risk exposure as a fraction of the known €820bn total (DNB)
  const totalPortfolio  = 820;
  const atRiskPortfolio = (rawAtRiskExp / rawTotalExp) * 820; // €bn

  return {
    scenarioId,
    totalPortfolio,
    atRiskPortfolio,
    pctUnderwater:  (atRiskMortgages / totalMortgages) * 100,
    avgLtvShift:    ltvShiftWeighted / totalMortgages,
    totalMortgages: Math.round(totalMortgages),
    atRiskMortgages: Math.round(atRiskMortgages),
    highRiskMunis,
  };
}

function computePortfolioOverview() {
  let totalM = 0, floodM = 0, foundM = 0;
  for (const gm of Object.values(gemeenteData)) {
    if (!gm.Population) continue;
    const m = (gm.Population / AVG_HH_SIZE) * MORTGAGE_PENETRATION;
    totalM += m;
    if (gm._flood_risk      > 0.40) floodM += m;
    if (gm._foundation_risk > 0.45) foundM += m;
  }
  return {
    pctFloodExposed: (floodM / totalM) * 100,
    pctFoundExposed: (foundM / totalM) * 100,
  };
}

// ── Building-level risk (PDOK BAG) ────────────────────────────────────────

function mortgageExposure(units) {
  // Proxy for fraction of units in this building likely covered by individual bank mortgages.
  // Single-unit panden are almost entirely private owner-occupied homes; large blocks are
  // predominantly woningcorporaties (social housing, ~29% of Dutch stock) financed via social
  // bonds — no individual LTV exposure. Source: CBS Woononderzoek 2021.
  const u = parseInt(units) || 1;
  if (u <= 1)  return { prob: 0.68, label: 'Likely private owner' };
  if (u <= 2)  return { prob: 0.52, label: 'Mostly owner-occupied' };
  if (u <= 4)  return { prob: 0.36, label: 'Mixed tenure' };
  if (u <= 10) return { prob: 0.20, label: 'Likely rental / mixed' };
  if (u <= 30) return { prob: 0.10, label: 'Likely social housing' };
  return               { prob: 0.04, label: 'Likely woningcorporatie' };
}

function computeBuildingRisk(identificatie, bouwjaar, units, scenarioId) {
  // BAG identificatie: first 4 digits = municipality code (e.g. '0363' → 'GM0363')
  const gmCode = 'GM' + (identificatie || '0000').substring(0, 4);
  const gm     = gemeenteData[gmCode];
  const sc     = SCENARIOS[scenarioId];

  let baseDiscount = 0, gmName = '—',
      floodRisk = 0.35, foundRisk = 0.28, droughtRisk = 0.22,
      heatRisk = 0.30, pluvialRisk = 0.35, elevM = null,
      muniMortgagePct = null, ownerOccPct = null,
      baseLtv = BASE_LTV_MEAN;

  if (gm) {
    baseDiscount    = municipalDiscount(gm, sc);
    gmName          = gm.Naam              || gmCode;
    floodRisk       = gm._flood_risk       || 0.35;
    foundRisk       = gm._foundation_risk  || 0.28;
    droughtRisk     = gm._drought_risk     || 0.22;
    heatRisk        = gm._heat_risk        || 0.30;
    pluvialRisk     = gm._pluvial_risk     || 0.35;
    elevM           = gm._elev_m           ?? null;
    muniMortgagePct = gm._mortgage_penetration != null
      ? Math.round(gm._mortgage_penetration * 100) : null;
    ownerOccPct     = gm._owner_occupied_pct ?? null;
    baseLtv         = gm._base_ltv          || BASE_LTV_MEAN;
  }

  // Older buildings: higher vulnerability (worse foundations, lower dike compliance)
  const year   = parseInt(bouwjaar) || 1975;
  const ageAdj = baseDiscount === 0 ? 0   // no effect at baseline
    : year < 1945 ? 0.10
    : year < 1965 ? 0.05
    : year < 1985 ? 0.01
    : year < 2005 ? -0.02
    : -0.05;

  const localDiscount = Math.min(Math.max(baseDiscount + ageAdj, 0), 0.60);
  const factor        = 1 / (1 - localDiscount);
  const stressedLTV   = Math.round(baseLtv * factor * 10) / 10;

  return {
    stressedLTV,
    baseLtv,
    gmCode,
    gmName,
    floodRisk:   Math.round(floodRisk   * 100),
    foundRisk:   Math.round(foundRisk   * 100),
    droughtRisk: Math.round(droughtRisk * 100),
    heatRisk:    Math.round(heatRisk    * 100),
    pluvialRisk: Math.round(pluvialRisk * 100),
    elevM:           elevM !== null ? Math.round(elevM * 10) / 10 : null,
    muniMortgagePct,
    ownerOccPct,
    year,
    underwater: stressedLTV > 100 ? 1 : 0,
    ...mortgageExposure(units),   // prob, label
  };
}

function annotateAndRenderBuildings(scenarioId) {
  if (!map || !map.getSource('buildings') || rawBuildingFeatures.length === 0) return;

  const annotated = rawBuildingFeatures.map(feat => ({
    ...feat,
    properties: {
      ...feat.properties,
      ...computeBuildingRisk(
        feat.properties.identificatie,
        feat.properties.bouwjaar,
        feat.properties.aantal_verblijfsobjecten,
        scenarioId,
      ),
    },
  }));

  map.getSource('buildings').setData({ type: 'FeatureCollection', features: annotated });
}

async function fetchBuildings() {
  if (!map) return;
  const zoom = map.getZoom();

  const hint      = document.getElementById('map-zoom-hint');
  const badge     = document.getElementById('building-count-badge');
  const bldLoad   = document.getElementById('building-loading');
  const zoomState = document.getElementById('mc-zoom-state');

  if (zoom < 12) {
    if (hint)      hint.classList.remove('hidden');
    if (badge)     badge.classList.add('hidden');
    if (zoomState) zoomState.textContent = 'Zoom in to see buildings';
    return;
  }
  if (hint) hint.classList.add('hidden');

  const b  = map.getBounds();
  const vW = b.getWest(), vS = b.getSouth(), vE = b.getEast(), vN = b.getNorth();

  // Trigger lazy loads for cities near the current viewport
  triggerLazyLoads(vW, vS, vE, vN);

  // ── Try preloaded city data first (instant — no network) ──────────────────
  for (const city of Object.values(preloadedCities)) {
    if (!city.features) continue;
    const [cW, cS, cE, cN] = city.bbox;
    const overlaps = vW < cE && vE > cW && vS < cN && vN > cS;
    if (!overlaps) continue;

    // Filter preloaded features to current viewport using polygon bbox overlap
    rawBuildingFeatures = city.features.filter(f => {
      const ring = f.geometry?.coordinates?.[0];
      if (!ring) return false;
      let minLng = Infinity, maxLng = -Infinity, minLat = Infinity, maxLat = -Infinity;
      for (const [lng, lat] of ring) {
        if (lng < minLng) minLng = lng; if (lng > maxLng) maxLng = lng;
        if (lat < minLat) minLat = lat; if (lat > maxLat) maxLat = lat;
      }
      return minLng <= vE && maxLng >= vW && minLat <= vN && maxLat >= vS;
    });

    annotateAndRenderBuildings(activeScenario);
    renderMapLegend(activeScenario);
    if (bldLoad) bldLoad.classList.add('hidden');
    if (badge) {
      badge.textContent = `${rawBuildingFeatures.length} buildings · ${city.name}`;
      badge.classList.remove('hidden');
    }
    if (zoomState) zoomState.textContent = `${rawBuildingFeatures.length} buildings · ${city.name}`;
    return;
  }

  // ── Fallback: parallel PDOK fetch (3×2 grid) for areas outside preloaded cities ──
  if (buildingFetchCtrl) buildingFetchCtrl.abort();
  buildingFetchCtrl = new AbortController();
  const signal = buildingFetchCtrl.signal;

  if (bldLoad) bldLoad.classList.remove('hidden');
  if (badge)   badge.classList.add('hidden');

  try {
    // Split viewport into 3 cols × 2 rows = 6 parallel requests → ~6× more coverage
    const COLS = 3, ROWS = 2;
    const cW = (vE - vW) / COLS, cH = (vN - vS) / ROWS;
    const cells = [];
    for (let r = 0; r < ROWS; r++)
      for (let c = 0; c < COLS; c++)
        cells.push({ s: vS + r*cH, w: vW + c*cW, n: vS + (r+1)*cH, e: vW + (c+1)*cW });

    const PDOK = 'https://service.pdok.nl/lv/bag/wfs/v2_0?service=WFS&version=2.0.0' +
                 '&request=GetFeature&typeName=bag:pand&outputFormat=application%2Fjson' +
                 '&count=1000&srsName=EPSG:4326';

    const results = await Promise.all(cells.map(cell =>
      fetch(`${PDOK}&bbox=${cell.s},${cell.w},${cell.n},${cell.e},EPSG:4326`, { signal })
        .then(r => r.ok ? r.json() : { features: [] })
        .then(d => d.features || [])
        .catch(() => [])
    ));

    // Merge, deduplicate, filter residential
    const seen = new Set();
    rawBuildingFeatures = results.flat().filter(f => {
      const id = f.properties?.identificatie;
      if (!id || seen.has(id)) return false;
      seen.add(id);
      return f.properties.status === 'Pand in gebruik' &&
             typeof f.properties.gebruiksdoel === 'string' &&
             f.properties.gebruiksdoel.includes('woonfunctie');
    });

    annotateAndRenderBuildings(activeScenario);
    renderMapLegend(activeScenario);
    if (badge) {
      badge.textContent = `${rawBuildingFeatures.length} buildings · BAG live`;
      badge.classList.remove('hidden');
    }
    if (zoomState) zoomState.textContent = `${rawBuildingFeatures.length} buildings · PDOK live`;
  } catch (err) {
    if (err.name !== 'AbortError') console.warn('Building fetch error:', err);
  } finally {
    if (bldLoad) bldLoad.classList.add('hidden');
  }
}

// ── LTV distribution data for chart ───────────────────────────────────────

function computeLTVDist(scenarioId) {
  const scenario = SCENARIOS[scenarioId];
  const values   = new Array(LTV_AXIS.length).fill(0);
  let totalWeight = 0;

  for (const gm of Object.values(gemeenteData)) {
    if (!gm.Population) continue;
    const mortgaged = (gm.Population / AVG_HH_SIZE) * MORTGAGE_PENETRATION;
    const woz       = gm._woz_value || 250;
    // Weight by mortgage volume (count × average value)
    const weight    = mortgaged * woz;
    totalWeight    += weight;

    const d      = municipalDiscount(gm, scenario);
    const factor = 1 / (1 - d);
    const mu     = (gm._base_ltv || BASE_LTV_MEAN) * factor;
    const sigma  = LTV_STD * factor;

    for (let i = 0; i < LTV_AXIS.length; i++) {
      values[i] += weight * gaussianPDF(LTV_AXIS[i], mu, sigma);
    }
  }

  const binWidth = LTV_AXIS[1] - LTV_AXIS[0]; // 2pp
  return values.map(v => (v / totalWeight) * binWidth * 100); // % of portfolio per bin
}

// ── Rendering ──────────────────────────────────────────────────────────────

function renderOverviewKPIs(overview) {
  document.getElementById('kpi-total-portfolio').textContent = '~€820bn';
  document.getElementById('kpi-flood-exposed').textContent =
    Math.round(overview.pctFloodExposed) + '%';
  document.getElementById('kpi-found-exposed').textContent =
    Math.round(overview.pctFoundExposed) + '%';
}

function renderStressMetrics(stats) {
  const hiClass  = stats.scenarioId === 'baseline' ? '' : `hi-${stats.scenarioId}`;
  const pctStr   = stats.pctUnderwater < 0.5
    ? '<0.5%'
    : stats.pctUnderwater.toFixed(1) + '%';
  const atRiskStr = stats.atRiskPortfolio < 1
    ? '<€1bn'
    : '€' + Math.round(stats.atRiskPortfolio) + 'bn';
  const shiftStr  = stats.avgLtvShift < 0.05
    ? '—'
    : '+' + stats.avgLtvShift.toFixed(1) + ' pp';
  const countStr  = stats.atRiskMortgages < 10000
    ? '<10k'
    : '~' + Math.round(stats.atRiskMortgages / 1000) + 'k';

  document.getElementById('stress-metrics').innerHTML = `
    <div class="sm-card ${hiClass}">
      <div class="sm-val">${pctStr}</div>
      <div class="sm-label">Mortgages underwater</div>
      <div class="sm-note">LTV &gt; 100% &nbsp;·&nbsp; ${countStr} mortgages</div>
    </div>
    <div class="sm-card ${hiClass}">
      <div class="sm-val">${atRiskStr}</div>
      <div class="sm-label">Negative-equity exposure</div>
      <div class="sm-note">Out of ~€820bn total portfolio</div>
    </div>
    <div class="sm-card">
      <div class="sm-val">${shiftStr}</div>
      <div class="sm-label">Avg. LTV shift</div>
      <div class="sm-note">Percentage points above baseline</div>
    </div>
    <div class="sm-card ${hiClass}">
      <div class="sm-val">${stats.highRiskMunis}</div>
      <div class="sm-label">High-risk municipalities</div>
      <div class="sm-note">&gt; 5% of local mortgages underwater</div>
    </div>
  `;
}

function renderScenarioDesc(scenarioId) {
  document.getElementById('scen-desc-bar').textContent = SCENARIOS[scenarioId].desc;
}

function renderMapCard(stats) {
  const el = document.getElementById('mc-metrics');
  if (!el) return;
  const s       = SCENARIOS[stats.scenarioId];
  const pctStr  = stats.pctUnderwater < 0.5 ? '<0.5%' : stats.pctUnderwater.toFixed(1) + '%';
  const expStr  = stats.atRiskPortfolio < 1 ? '<€1bn' : '€' + Math.round(stats.atRiskPortfolio) + 'bn';
  const valCol  = s.color;
  el.innerHTML = `
    <div class="mc-metric">
      <div class="mc-metric-val" style="color:${valCol}">${pctStr}</div>
      <div class="mc-metric-label">mortgages<br>underwater</div>
    </div>
    <div class="mc-metric">
      <div class="mc-metric-val">${expStr}</div>
      <div class="mc-metric-label">at negative<br>equity</div>
    </div>
  `;
}

// ── Chart.js ───────────────────────────────────────────────────────────────

const verticalLinePlugin = {
  id: 'vLine',
  afterDatasetsDraw(chart) {
    const { ctx, scales: { x }, chartArea: { top, bottom } } = chart;
    const px = x.getPixelForValue(100);
    ctx.save();
    ctx.beginPath();
    ctx.moveTo(px, top);
    ctx.lineTo(px, bottom);
    ctx.lineWidth  = 1.5;
    ctx.strokeStyle = '#dc2626';
    ctx.setLineDash([5, 3]);
    ctx.stroke();
    ctx.fillStyle = '#dc2626';
    ctx.font = '500 10px Inter, sans-serif';
    ctx.fillText('100% LTV', px + 4, top + 13);
    ctx.restore();
  },
};

function initChart(baselineDist) {
  const ctx = document.getElementById('ltv-chart').getContext('2d');
  ltvChart  = new Chart(ctx, {
    type: 'line',
    data: {
      labels: LTV_AXIS,
      datasets: [
        {
          label: 'Baseline',
          data: baselineDist,
          borderColor: '#94a3b8',
          backgroundColor: 'rgba(148,163,184,0.15)',
          fill: true, tension: 0.35, pointRadius: 0, borderWidth: 1.5, order: 2,
        },
        {
          label: 'Scenario',
          data: baselineDist.slice(),
          borderColor: '#94a3b8',
          backgroundColor: 'rgba(148,163,184,0.15)',
          fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2, order: 1,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 350, easing: 'easeInOutQuad' },
      scales: {
        x: {
          title: { display: true, text: 'Loan-to-Value Ratio (%)', font: { size: 11, family: 'Inter' }, color: '#64748b' },
          grid: { color: 'rgba(0,0,0,0.04)' },
          ticks: {
            maxTicksLimit: 14, font: { size: 10, family: 'Inter' }, color: '#94a3b8',
            callback: v => v % 10 === 0 ? v + '%' : '',
          },
        },
        y: {
          title: { display: true, text: 'Share of portfolio (%)', font: { size: 11, family: 'Inter' }, color: '#64748b' },
          grid: { color: 'rgba(0,0,0,0.04)' },
          ticks: { font: { size: 10, family: 'Inter' }, color: '#94a3b8', callback: v => v.toFixed(1) + '%' },
          beginAtZero: true,
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: items => `LTV ≈ ${items[0].label}%`,
            label: item => `${item.dataset.label}: ${Number(item.raw).toFixed(2)}% of portfolio`,
          },
        },
      },
    },
    plugins: [verticalLinePlugin],
  });
}

function updateChart(scenarioId) {
  if (!ltvChart) return;
  const s    = SCENARIOS[scenarioId];
  const dist = computeLTVDist(scenarioId);

  // Parse hex color to rgba for fill
  const hex = s.colorDark;
  const r   = parseInt(hex.slice(1, 3), 16);
  const g   = parseInt(hex.slice(3, 5), 16);
  const b   = parseInt(hex.slice(5, 7), 16);

  ltvChart.data.datasets[1].data            = dist;
  ltvChart.data.datasets[1].label           = s.label;
  ltvChart.data.datasets[1].borderColor     = s.colorDark;
  ltvChart.data.datasets[1].backgroundColor = `rgba(${r},${g},${b},0.18)`;

  // Legend chip
  const swatch = document.getElementById('cleg-scenario-swatch');
  swatch.style.background = s.color;
  document.getElementById('cleg-scenario-label').textContent = s.label;

  const scenItem = document.getElementById('cleg-scenario-item');
  if (scenarioId === 'baseline') {
    scenItem.style.opacity = '0';
  } else {
    scenItem.style.opacity = '1';
  }

  ltvChart.update();
}

// ── Map ────────────────────────────────────────────────────────────────────

function mapColorExpr(scenarioId) {
  const prop = `_uw_${scenarioId}`;
  // step expression: threshold breakpoints → colors
  return ['step', ['get', prop],
    '#1e3a5f',  // < 1%
     1, '#ecfdf5',
     3, '#fde68a',
     6, '#fb923c',
    12, '#ef4444',
    20, '#991b1b',
  ];
}

function renderMapLegend(scenarioId) {
  const s    = SCENARIOS[scenarioId];
  const zoom = map ? map.getZoom() : 0;

  if (zoom >= 12 && rawBuildingFeatures.length > 0) {
    // Building-level legend
    document.getElementById('map-legend').innerHTML = `
      <div class="legend-title">Stressed LTV per Building</div>
      <div style="font-size:0.7rem;color:#475569;margin-bottom:8px">${s.label}</div>
      <div class="legend-item"><span class="legend-swatch" style="background:#22c55e"></span>&lt; 80%  · safe</div>
      <div class="legend-item"><span class="legend-swatch" style="background:#fbbf24"></span>80 – 90%  · moderate</div>
      <div class="legend-item"><span class="legend-swatch" style="background:#f97316"></span>90 – 100%  · high risk</div>
      <div class="legend-item"><span class="legend-swatch" style="background:#ef4444"></span>&gt; 100%  · underwater</div>
    `;
  } else {
    // Municipality-level legend
    document.getElementById('map-legend').innerHTML = `
      <div class="legend-title">% Mortgages Underwater</div>
      <div style="font-size:0.7rem;color:#475569;margin-bottom:8px">${s.label}</div>
      <div class="legend-item"><span class="legend-swatch" style="background:#ecfdf5;border:1px solid #334155"></span>&lt; 3%</div>
      <div class="legend-item"><span class="legend-swatch" style="background:#fde68a"></span>3 – 6%</div>
      <div class="legend-item"><span class="legend-swatch" style="background:#fb923c"></span>6 – 12%</div>
      <div class="legend-item"><span class="legend-swatch" style="background:#ef4444"></span>12 – 20%</div>
      <div class="legend-item"><span class="legend-swatch" style="background:#991b1b"></span>&gt; 20%</div>
    `;
  }
}

function updateMap(scenarioId) {
  if (!map || !map.getLayer('fill')) return;
  map.setPaintProperty('fill', 'fill-color', mapColorExpr(scenarioId));
  if (rawBuildingFeatures.length > 0) annotateAndRenderBuildings(scenarioId);
  renderMapLegend(scenarioId);
}

function annotateGeoJSON(geoJSON) {
  const byCode = {};
  const byName = {};
  for (const [code, gm] of Object.entries(gemeenteData)) {
    byCode[code] = gm;
    if (gm.Naam) byName[gm.Naam.toLowerCase()] = gm;
  }

  for (const feat of geoJSON.features) {
    const p    = feat.properties;
    const code = p.statcode || p.GM_CODE  || '';
    const name = (p.statnaam || p.GM_NAAM || '').toLowerCase();
    const gm   = byCode[code] || byName[name];

    if (gm) {
      p._flood_risk      = gm._flood_risk;
      p._foundation_risk = gm._foundation_risk;
      p._drought_risk    = gm._drought_risk;
      p._heat_risk            = gm._heat_risk;
      p._pluvial_risk         = gm._pluvial_risk;
      p._elev_m               = gm._elev_m;
      p._mortgage_penetration = gm._mortgage_penetration;
      p._owner_occupied_pct   = gm._owner_occupied_pct;
      p._woz_value            = gm._woz_value;
      for (const id of Object.keys(SCENARIOS)) {
        p[`_uw_${id}`] = gm[`_uw_${id}`] || 0;
      }
    } else {
      p._flood_risk = 0.35; p._foundation_risk = 0.28; p._drought_risk = 0.22;
      for (const id of Object.keys(SCENARIOS)) p[`_uw_${id}`] = 0;
    }
  }
  return geoJSON;
}

async function initMap(geoJSON) {
  map = new maplibregl.Map({
    container: 'map',
    style: {
      version: 8,
      sources: {},
      layers: [{ id: 'bg', type: 'background', paint: { 'background-color': '#0f172a' } }],
    },
    center: [5.28, 52.18],
    zoom: 6.3,
    attributionControl: false,
  });

  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'bottom-right');

  await new Promise(resolve => map.on('load', resolve));

  // ── Municipality choropleth layers (fade out when zoomed in) ──────────────
  map.addSource('g', { type: 'geojson', data: geoJSON });

  map.addLayer({
    id: 'fill', type: 'fill', source: 'g',
    paint: {
      'fill-color': mapColorExpr(activeScenario),
      // Fade out as buildings take over
      'fill-opacity': ['interpolate', ['linear'], ['zoom'], 12, 0.88, 14, 0],
    },
  });

  map.addLayer({
    id: 'line', type: 'line', source: 'g',
    paint: {
      'line-color': '#0f172a',
      'line-width': 0.35,
      'line-opacity': ['interpolate', ['linear'], ['zoom'], 12, 0.8, 14, 0],
    },
  });

  // ── Building layers (from PDOK BAG, fade in when zoomed in) ──────────────
  map.addSource('buildings', { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });

  map.addLayer({
    id: 'bld-fill', type: 'fill', source: 'buildings',
    paint: {
      'fill-color': ['step', ['get', 'stressedLTV'],
        '#22c55e',   // < 80  safe
        80,  '#fbbf24', // 80-90 moderate
        90,  '#f97316', // 90-100 high risk
        100, '#ef4444', // > 100  underwater
      ],
      // Scale opacity by unit count so apartment blocks stand out more than single houses
      'fill-opacity': ['interpolate', ['linear'], ['zoom'], 12, 0, 13,
        ['interpolate', ['linear'],
          ['coalesce', ['get', 'aantal_verblijfsobjecten'], 1],
          1, 0.60,
          5, 0.75,
          20, 0.88,
          50, 0.95,
        ],
      ],
    },
  });

  map.addLayer({
    id: 'bld-line', type: 'line', source: 'buildings',
    paint: {
      'line-color': '#0f172a',
      'line-width': ['interpolate', ['linear'], ['zoom'], 12, 0.2, 17, 1.2],
      'line-opacity': 0.45,
    },
  });

  // ── Popup (shared between both layers) ────────────────────────────────────
  const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });

  // Municipality hover
  map.on('mousemove', 'fill', e => {
    if (map.getZoom() >= 13) return; // buildings cover this zoom
    map.getCanvas().style.cursor = 'crosshair';
    const p  = e.features[0].properties;
    const uw = p[`_uw_${activeScenario}`];
    const fr   = ((p._flood_risk      || 0) * 100).toFixed(0);
    const fn   = ((p._foundation_risk || 0) * 100).toFixed(0);
    const dr   = ((p._drought_risk    || 0) * 100).toFixed(0);
    const hr   = ((p._heat_risk       || 0) * 100).toFixed(0);
    const pr   = ((p._pluvial_risk    || 0) * 100).toFixed(0);
    const nm   = p.statnaam || p.GM_NAAM || '—';
    const elev = p._elev_m != null ? p._elev_m : null;
    popup.setLngLat(e.lngLat).setHTML(muniPopupHTML(nm, uw, fr, fn, dr, hr, pr, elev)).addTo(map);
  });
  map.on('mouseleave', 'fill', () => { map.getCanvas().style.cursor = ''; popup.remove(); });

  // Building hover
  map.on('mousemove', 'bld-fill', e => {
    map.getCanvas().style.cursor = 'pointer';
    const p = e.features[0].properties;
    popup.setLngLat(e.lngLat).setHTML(buildingPopupHTML(p)).addTo(map);
  });
  map.on('mouseleave', 'bld-fill', () => { map.getCanvas().style.cursor = ''; popup.remove(); });

  // ── Fetch buildings on map move/zoom ──────────────────────────────────────
  let fetchTimer = null;
  const scheduleFetch = () => {
    clearTimeout(fetchTimer);
    fetchTimer = setTimeout(fetchBuildings, 300); // debounce
  };
  map.on('moveend', scheduleFetch);
  map.on('zoomend', () => {
    renderMapLegend(activeScenario);
    scheduleFetch();
  });

  // City fly-to buttons
  const CITY_VIEWS = {
    amsterdam: { center: [4.895, 52.373], zoom: 14 },
    rotterdam: { center: [4.477, 51.922], zoom: 14 },
    den_haag:  { center: [4.310, 52.078], zoom: 14 },
    utrecht:   { center: [5.121, 52.092], zoom: 14 },
    eindhoven: { center: [5.478, 51.441], zoom: 14 },
  };

  document.getElementById('mc-cities').addEventListener('click', e => {
    const btn = e.target.closest('.mc-city-btn');
    if (!btn) return;
    const view = CITY_VIEWS[btn.dataset.city];
    if (!view) return;
    // Mark active
    document.querySelectorAll('.mc-city-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    map.flyTo({ center: view.center, zoom: view.zoom, duration: 1600 });
  });

  renderMapLegend(activeScenario);
  document.getElementById('loading-overlay').classList.add('hidden');
}

function muniPopupHTML(name, uw, fr, fn, dr, hr, pr, elev) {
  const elevStr = elev != null
    ? `${elev >= 0 ? '+' : ''}${Number(elev).toFixed(1)} m NAP`
    : null;
  return `
    <div style="font-family:Inter,sans-serif;font-size:12px;padding:8px 10px;background:#1e293b;color:#e2e8f0;border-radius:8px;border:1px solid #334155;line-height:1.6">
      <div style="font-family:'Space Grotesk',sans-serif;font-size:13px;font-weight:700;margin-bottom:4px">${name}</div>
      <div style="color:#94a3b8">Mortgages underwater</div>
      <div style="font-size:15px;font-weight:700;color:#f8fafc">${uw != null ? Number(uw).toFixed(1) + '%' : '—'}</div>
      <div style="margin-top:6px;display:grid;grid-template-columns:1fr 1fr 1fr;gap:3px 6px">
        <span style="color:#94a3b8;font-size:11px">Flood<br><span style="color:#e2e8f0;font-weight:600">${fr}%</span></span>
        <span style="color:#94a3b8;font-size:11px">Found.<br><span style="color:#e2e8f0;font-weight:600">${fn}%</span></span>
        <span style="color:#94a3b8;font-size:11px">Drought<br><span style="color:#e2e8f0;font-weight:600">${dr}%</span></span>
        <span style="color:#94a3b8;font-size:11px">Heat<br><span style="color:#fca5a5;font-weight:600">${hr}%</span></span>
        <span style="color:#94a3b8;font-size:11px">Pluvial<br><span style="color:#93c5fd;font-weight:600">${pr}%</span></span>
        ${elevStr ? `<span style="color:#64748b;font-size:10px">Elevation<br><span style="color:#7dd3fc;font-weight:600">${elevStr}</span></span>` : '<span></span>'}
      </div>
    </div>`;
}

function buildingPopupHTML(p) {
  const ltv   = p.stressedLTV != null ? Number(p.stressedLTV) : null;
  const units = parseInt(p.aantal_verblijfsobjecten) || 1;
  const areaMin = parseInt(p.oppervlakte_min) || null;
  const areaMax = parseInt(p.oppervlakte_max) || null;
  const areaStr = areaMin && areaMax
    ? (areaMin === areaMax ? `${areaMin} m²` : `${areaMin}–${areaMax} m²`)
    : areaMin ? `${areaMin}+ m²` : '—';

  const statusColor = ltv == null ? '#94a3b8'
    : ltv >= 100 ? '#ef4444'
    : ltv >= 90  ? '#f97316'
    : ltv >= 80  ? '#fbbf24'
    : '#22c55e';
  const statusLabel = ltv == null ? '—'
    : ltv >= 100 ? 'Underwater'
    : ltv >= 90  ? 'High risk'
    : ltv >= 80  ? 'Moderate risk'
    : 'Low risk';

  const unitsAtRisk = ltv != null && ltv >= 100 ? units : 0;
  const scenLabel   = SCENARIOS[activeScenario].label;
  const elevM       = p.elevM != null ? p.elevM : null;
  const elevStr     = elevM !== null
    ? `${elevM >= 0 ? '+' : ''}${Number(elevM).toFixed(1)} m NAP`
    : null;
  const mortProb    = p.prob  != null ? Math.round(p.prob * 100) : null;
  const mortLabel   = p.label || null;
  const mortColor   = mortProb == null ? '#64748b'
    : mortProb >= 50 ? '#4ade80'
    : mortProb >= 25 ? '#fbbf24'
    : '#f87171';

  return `
    <div style="font-family:Inter,sans-serif;font-size:12px;padding:10px 12px;background:#1e293b;color:#e2e8f0;border-radius:8px;border:1px solid #334155;line-height:1.6;min-width:200px">
      <div style="font-family:'Space Grotesk',sans-serif;font-size:13px;font-weight:700;margin-bottom:6px">${p.gmName || '—'}</div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:2px 8px;color:#94a3b8;margin-bottom:8px">
        <span>Built: <span style="color:#e2e8f0">${p.year || '—'}</span></span>
        <span>Floor area: <span style="color:#e2e8f0">${areaStr}</span></span>
        <span>Flood: <span style="color:#e2e8f0">${p.floodRisk || 0}%</span></span>
        <span>Foundation: <span style="color:#e2e8f0">${p.foundRisk || 0}%</span></span>
        <span>Drought: <span style="color:#e2e8f0">${p.droughtRisk || 0}%</span></span>
        <span>Heat: <span style="color:#fca5a5">${p.heatRisk || 0}%</span></span>
        <span style="grid-column:1/-1">Pluvial: <span style="color:#93c5fd">${p.pluvialRisk || 0}%</span></span>
      </div>
      ${elevStr ? `<div style="margin-bottom:8px;padding:4px 6px;background:rgba(125,211,252,0.08);border-radius:5px;border-left:2px solid #7dd3fc">
        <span style="color:#64748b;font-size:10.5px">Area elevation (AHN4) </span><span style="color:#7dd3fc;font-weight:600">${elevStr}</span>
      </div>` : ''}

      <div style="background:rgba(15,23,42,0.6);border-radius:6px;padding:6px 8px;margin-bottom:8px;display:flex;align-items:center;gap:10px">
        <span style="font-size:10px;color:#475569">Residential<br>units</span>
        <span style="font-size:22px;font-weight:700;color:#e2e8f0;line-height:1">${units}</span>
        ${unitsAtRisk > 0 ? `<span style="font-size:10px;color:#ef4444;font-weight:600;margin-left:auto">${unitsAtRisk} unit${unitsAtRisk > 1 ? 's' : ''}<br>underwater</span>` : ''}
      </div>

      ${mortProb != null ? `
      <div style="background:rgba(15,23,42,0.5);border-radius:6px;padding:6px 8px;margin-bottom:8px;border-left:2px solid ${mortColor}">
        <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:3px">
          <span style="font-size:10px;color:#64748b">Mortgage exposure est.</span>
          <span style="font-size:13px;font-weight:700;color:${mortColor}">~${mortProb}%</span>
        </div>
        <div style="background:#1e293b;border-radius:3px;height:4px;margin-bottom:3px">
          <div style="background:${mortColor};width:${mortProb}%;height:100%;border-radius:3px;opacity:0.8"></div>
        </div>
        <div style="font-size:9.5px;color:#475569">${mortLabel} · unit-count proxy</div>
        ${p.muniMortgagePct != null ? `<div style="font-size:9px;color:#475569;margin-top:2px">Municipality avg: <span style="color:#94a3b8">${p.muniMortgagePct}%</span>${p.ownerOccPct != null ? ` (${p.ownerOccPct}% owner-occ · CBS)` : ''}</div>` : ''}
      </div>` : ''}

      <div style="padding-top:8px;border-top:1px solid #334155">
        <div style="font-size:10px;color:#475569;margin-bottom:2px">${scenLabel}</div>
        <div style="font-size:20px;font-weight:700;color:${statusColor};line-height:1.1">${ltv != null ? ltv.toFixed(1) + '%' : '—'} <span style="font-size:11px;font-weight:400">stressed LTV</span></div>
        <div style="font-size:11px;color:${statusColor};font-weight:600">${statusLabel}</div>
        ${p.baseLtv != null ? `<div style="font-size:9.5px;color:#475569;margin-top:3px">Base LTV: <span style="color:#94a3b8">${Number(p.baseLtv).toFixed(1)}%</span> <span style="color:#334155">(CBS/DNB est.)</span></div>` : ''}
      </div>
    </div>`;
}

// ── Scenario switching ─────────────────────────────────────────────────────

function switchScenario(id) {
  activeScenario = id;
  // Sync both the map card buttons and the stress-test tabs
  document.querySelectorAll('.scen-tab, .mc-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.scenario === id);
  });
  const stats = computeScenarioStats(id);
  renderStressMetrics(stats);
  renderScenarioDesc(id);
  renderMapCard(stats);
  updateChart(id);
  updateMap(id);
}

// ── Init ───────────────────────────────────────────────────────────────────

async function init() {
  const msg = document.getElementById('loading-message');
  msg.textContent = 'Loading municipal data…';

  // Eagerly load Amsterdam (fly-to demo target) — no await so it doesn't block map init
  loadCityBuildings('amsterdam');

  // Parallel fetch: municipality data, climate risk, GeoJSON boundaries
  const [nlData, riskData, geoJSON] = await Promise.all([
    fetch('data/nl_data.json').then(r => r.json()),
    fetch('data/climate_risk.json').then(r => r.json()),
    fetch('data/gemeenten.geojson').then(r => r.json()).catch(() => null),
  ]);

  // Flatten nl_data.json → gemeenteData keyed by GM code
  const rawMunis = nlData['Gemeente'] || {};
  for (const [code, gm] of Object.entries(rawMunis)) {
    if (gm && gm.Naam) {
      // Ensure _woz_value is available at top level
      if (!gm._woz_value && gm.Housing) {
        gm._woz_value = gm.Housing['Avg WOZ value (x€1k)'] || 250;
      }
      if (!gm._woz_value) gm._woz_value = 250;
      gemeenteData[code] = gm;
    }
  }

  msg.textContent = 'Computing risk scores…';
  computeRiskScores(riskData);

  msg.textContent = 'Running stress scenarios…';
  precomputeAllScenarios();

  // Overview KPIs (static)
  const overview = computePortfolioOverview();
  renderOverviewKPIs(overview);

  // Chart
  const baselineDist = computeLTVDist('baseline');
  initChart(baselineDist);
  updateChart('baseline');

  // Stress metrics + scenario description + map card
  const baselineStats = computeScenarioStats('baseline');
  renderStressMetrics(baselineStats);
  renderScenarioDesc('baseline');
  renderMapCard(baselineStats);

  // Map
  if (geoJSON) {
    msg.textContent = 'Rendering map…';
    const annotated = annotateGeoJSON(geoJSON);
    await initMap(annotated);
  } else {
    document.getElementById('loading-overlay').classList.add('hidden');
  }

  // Scenario tab click handlers
  document.getElementById('scenario-tabs').addEventListener('click', e => {
    const btn = e.target.closest('.scen-tab');
    if (btn) switchScenario(btn.dataset.scenario);
  });

  // Map card scenario button handlers
  document.getElementById('mc-btns').addEventListener('click', e => {
    const btn = e.target.closest('.mc-btn');
    if (btn) switchScenario(btn.dataset.scenario);
  });
}

init().catch(err => {
  console.error('Init failed:', err);
  document.getElementById('loading-message').textContent = 'Failed to load data.';
});
