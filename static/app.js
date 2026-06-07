document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('parse-form');
    const uploadZone = document.getElementById('upload-zone');
    const fileInput = document.getElementById('file-input');
    const processBtn = document.getElementById('process-btn');
    const loadDemoBtn = document.getElementById('load-demo-btn');
    const btnText = document.querySelector('.btn-text');
    const spinner = document.querySelector('.spinner');
    const confSlider = document.getElementById('confidence');
    const confVal = document.getElementById('conf-val');
    const tabs = document.querySelectorAll('.tab');
    const contents = document.querySelectorAll('.tab-content');
    const themeToggle = document.getElementById('theme-toggle');

    const summaryBar = document.getElementById('summary-bar');
    const resultImage = document.getElementById('result-image');
    const overlayContainer = document.getElementById('overlay-container');
    const overlayEmpty = document.getElementById('overlay-empty');

    const financeDistrict = document.getElementById('finance-district');
    const financeEstate = document.getElementById('finance-estate');
    const financeRecalc = document.getElementById('finance-recalc');
    const financeContainer = document.getElementById('finance-container');
    const financeEmpty = document.getElementById('finance-empty');
    const financeHero = document.getElementById('finance-hero');

    const roomsContainer = document.getElementById('rooms-container');
    const roomsEmpty = document.getElementById('rooms-empty');
    const roomsTbody = document.getElementById('rooms-tbody');

    const complianceEmpty = document.getElementById('compliance-empty');
    const complianceContainer = document.getElementById('compliance-container');
    const complianceCards = document.getElementById('compliance-cards');
    const runComplianceBtn = document.getElementById('run-compliance-btn');

    const jsonOutput = document.getElementById('json-output');
    const jsonContainer = document.getElementById('json-container');
    const jsonEmpty = document.getElementById('json-empty');
    const copyBtn = document.getElementById('copy-json');

    const parseProgress = document.getElementById('parse-progress');
    const parseSteps = parseProgress ? parseProgress.querySelectorAll('.parse-step') : [];
    const lightbox = document.getElementById('lightbox');
    const lightboxImg = document.getElementById('lightbox-img');
    const lightboxClose = document.getElementById('lightbox-close');
    const toastStack = document.getElementById('toast-stack');

    let currentGraph = null;
    let currentFile = null;
    let isDemoMode = false;
    let demoOverlayUrl = null;
    let currentOverlayBlob = null;
    let progressTimer = null;

    const hkd = (n) => `HKD ${Number(n).toLocaleString('en-HK', { maximumFractionDigits: 0 })}`;

    function toast(message, type = '') {
        if (!toastStack) { return; }
        const el = document.createElement('div');
        el.className = `toast ${type}`.trim();
        el.textContent = message;
        toastStack.appendChild(el);
        setTimeout(() => {
            el.classList.add('fade-out');
            setTimeout(() => el.remove(), 300);
        }, 4000);
    }

    function startParseProgress() {
        if (!parseProgress) { return; }
        overlayEmpty.classList.add('hidden');
        overlayContainer.classList.add('hidden');
        parseProgress.classList.remove('hidden');
        parseSteps.forEach(s => s.classList.remove('active', 'done'));
        let i = 0;
        const advance = () => {
            parseSteps.forEach((s, idx) => {
                s.classList.toggle('done', idx < i);
                s.classList.toggle('active', idx === i);
            });
            i = Math.min(i + 1, parseSteps.length - 1);
        };
        advance();
        progressTimer = setInterval(advance, 1200);
    }

    function stopParseProgress() {
        if (progressTimer) { clearInterval(progressTimer); progressTimer = null; }
        if (parseProgress) { parseProgress.classList.add('hidden'); }
    }

    function animateCount(el, target, formatter) {
        const start = 0;
        const duration = 900;
        const startTime = performance.now();
        function frame(now) {
            const p = Math.min((now - startTime) / duration, 1);
            const eased = 1 - Math.pow(1 - p, 3);
            el.textContent = formatter(Math.round(start + (target - start) * eased));
            if (p < 1) { requestAnimationFrame(frame); }
        }
        requestAnimationFrame(frame);
    }

    const CATEGORY_CLASS = {
        living: 'cat-living', livingroom: 'cat-living', lounge: 'cat-living',
        bedroom: 'cat-bedroom', bed: 'cat-bedroom', master: 'cat-bedroom',
        kitchen: 'cat-kitchen', dining: 'cat-kitchen',
        bathroom: 'cat-bathroom', bath: 'cat-bathroom', toilet: 'cat-bathroom', wc: 'cat-bathroom',
        hallway: 'cat-circulation', corridor: 'cat-circulation', circulation: 'cat-circulation',
        entrance: 'cat-circulation', stairs: 'cat-circulation', lobby: 'cat-circulation',
        utility: 'cat-utility', storage: 'cat-utility', store: 'cat-utility', closet: 'cat-utility',
        balcony: 'cat-outdoor', terrace: 'cat-outdoor', garden: 'cat-outdoor',
    };
    function categoryClass(category) {
        const key = String(category || '').toLowerCase().replace(/[^a-z]/g, '');
        for (const k in CATEGORY_CLASS) {
            if (key.includes(k)) { return CATEGORY_CLASS[k]; }
        }
        return '';
    }

    themeToggle.addEventListener('click', () => document.body.classList.toggle('light-theme'));
    confSlider.addEventListener('input', (e) => {
        confVal.textContent = parseFloat(e.target.value).toFixed(2);
    });

    tabs.forEach(tab => {
        tab.addEventListener('click', () => {
            tabs.forEach(t => t.classList.remove('active'));
            contents.forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            document.getElementById(tab.dataset.target).classList.add('active');
        });
    });

    uploadZone.addEventListener('click', () => fileInput.click());
    uploadZone.addEventListener('dragover', (e) => { e.preventDefault(); uploadZone.classList.add('dragover'); });
    uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('dragover'));
    uploadZone.addEventListener('drop', (e) => {
        e.preventDefault();
        uploadZone.classList.remove('dragover');
        if (e.dataTransfer.files.length) {
            fileInput.files = e.dataTransfer.files;
            updateFileState();
        }
    });
    fileInput.addEventListener('change', updateFileState);

    function updateFileState() {
        if (fileInput.files.length > 0) {
            uploadZone.classList.add('has-file');
            document.querySelector('.upload-text').textContent = fileInput.files[0].name;
            processBtn.disabled = false;
            isDemoMode = false;
        } else if (!isDemoMode) {
            uploadZone.classList.remove('has-file');
            document.querySelector('.upload-text').innerHTML = 'Drag & drop or <span class="browse">browse</span>';
            processBtn.disabled = true;
        }
    }

    function setLoading(loading) {
        btnText.classList.toggle('hidden', loading);
        spinner.classList.toggle('hidden', !loading);
        processBtn.disabled = loading;
        loadDemoBtn.disabled = loading;
    }

    function switchTab(targetId) {
        tabs.forEach(t => {
            t.classList.toggle('active', t.dataset.target === targetId);
        });
        contents.forEach(c => {
            c.classList.toggle('active', c.id === targetId);
        });
    }

    function updateSummary(graph, demo = false) {
        const spaces = graph.spaces || graph.zones || [];
        const meta = graph.meta || {};
        const totalArea = meta.total_floor_area_m2 ||
            spaces.reduce((s, sp) => s + (sp.area_m2 || 0), 0);
        document.getElementById('chip-rooms').textContent = `${spaces.length} rooms`;
        document.getElementById('chip-area').textContent = `${Math.round(totalArea)} m²`;
        document.getElementById('chip-parser').textContent = meta.parser || 'parsed';
        document.getElementById('chip-demo').classList.toggle('hidden', !demo);
        summaryBar.classList.remove('hidden');
    }

    async function renderOverlayFromApi(file, graph) {
        const fd = new FormData();
        fd.append('file', file, file.name || 'upload.jpg');
        fd.append('graph_json', JSON.stringify(graph));
        const r = await fetch('/overlay', { method: 'POST', body: fd });
        if (!r.ok) {
            const detail = await r.text();
            throw new Error(detail || `Overlay render failed (${r.status})`);
        }
        const blob = await r.blob();
        return URL.createObjectURL(blob);
    }

    function showOverlay(url) {
        if (currentOverlayBlob) {
            URL.revokeObjectURL(currentOverlayBlob);
            currentOverlayBlob = null;
        }
        if (url && url.startsWith('blob:')) {
            currentOverlayBlob = url;
        }
        overlayEmpty.classList.add('hidden');
        overlayContainer.classList.remove('hidden');
        resultImage.src = url;
    }

    function showUploadPreview(file) {
        const reader = new FileReader();
        reader.onload = (e) => showOverlay(e.target.result);
        reader.readAsDataURL(file);
    }

    function parseApiError(text) {
        try {
            const j = JSON.parse(text);
            return j.detail || text;
        } catch (_) {
            return text;
        }
    }

    function renderRooms(graph) {
        const spaces = (graph.spaces || []).slice().sort((a, b) =>
            (b.area_m2 || 0) - (a.area_m2 || 0));
        if (!spaces.length) {
            roomsEmpty.classList.remove('hidden');
            roomsContainer.classList.add('hidden');
            return;
        }
        roomsEmpty.classList.add('hidden');
        roomsContainer.classList.remove('hidden');
        roomsTbody.innerHTML = '';
        let total = 0;
        spaces.forEach((sp, idx) => {
            const area = sp.area_m2 || 0;
            total += area;
            const tr = document.createElement('tr');
            tr.style.animationDelay = `${Math.min(idx * 30, 400)}ms`;
            const catClass = categoryClass(sp.category);
            tr.innerHTML = `
                <td>${esc(sp.name || sp.id)}</td>
                <td><span class="cat-badge ${catClass}">${esc(sp.category || '—')}</span></td>
                <td>${area ? area.toFixed(1) : '—'}</td>
                <td>${sp.confidence ? (sp.confidence * 100).toFixed(0) + '%' : '—'}</td>`;
            roomsTbody.appendChild(tr);
        });
        document.getElementById('rooms-total-area').textContent = total.toFixed(1);
        document.getElementById('rooms-count').textContent = `${spaces.length} rooms`;
    }

    function esc(s) {
        const d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    function showJson(graph) {
        jsonEmpty.classList.add('hidden');
        jsonContainer.classList.remove('hidden');
        jsonOutput.textContent = JSON.stringify(graph, null, 2);
    }

    async function loadEstates(district) {
        financeEstate.innerHTML = '<option value="">District average</option>';
        try {
            const r = await fetch(`/finance/districts?district=${encodeURIComponent(district)}`);
            if (!r.ok) return;
            const data = await r.json();
            (data.estates || []).sort().forEach(name => {
                const opt = document.createElement('option');
                opt.value = name;
                opt.textContent = name;
                financeEstate.appendChild(opt);
            });
        } catch (_) {}
    }

    async function updateFinance(graph) {
        financeEmpty.classList.add('hidden');
        financeContainer.classList.remove('hidden');
        await fetchPropertyFinance(graph);
    }

    async function fetchPropertyFinance(graph) {
        const district = financeDistrict.value;
        const estate = financeEstate.value || null;
        try {
            const r = await fetch('/finance/property', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ graph, district, estate }),
            });
            if (!r.ok) {
                const err = await r.json();
                throw new Error(err.detail || r.statusText);
            }
            const fin = await r.json();
            financeHero.classList.remove('hidden');
            const heroValueEl = document.getElementById('fin-hero-value');
            animateCount(heroValueEl, fin.property_value_hkd, hkd);
            financeHero.classList.remove('pop');
            void financeHero.offsetWidth;
            financeHero.classList.add('pop');
            const estateLabel = fin.estate || 'district average';
            document.getElementById('fin-hero-sub').textContent =
                `${fin.district} · ${estateLabel} · ${fin.annual_roi_pct}% ROI`;
            document.getElementById('fin-area').textContent =
                `${fin.area_m2} m² (${fin.area_sqft} ft²)`;
            document.getElementById('fin-benefit').textContent = hkd(fin.annual_benefit_hkd);
            document.getElementById('fin-roi').textContent = `${fin.annual_roi_pct}%`;
            document.getElementById('fin-property-note').textContent =
                `HKD ${fin.price_per_sqft_hkd.toLocaleString()}/ft² sale · HKD ${fin.rent_per_sqft_month_hkd}/ft²/mo rent`;
        } catch (err) {
            financeHero.classList.remove('hidden');
            document.getElementById('fin-hero-value').textContent = '—';
            document.getElementById('fin-property-note').textContent =
                `Valuation failed: ${err.message}. Demo may need district data in config/.`;
        }
    }

    async function runCompliance() {
        complianceEmpty.classList.add('hidden');
        complianceContainer.classList.remove('hidden');
        complianceCards.innerHTML = '<p class="compliance-loading">Running TIA-942 checks…</p>';

        const subtitleEl = document.getElementById('compliance-subtitle');

        try {
            await fetch('/compliance/init-demo', { method: 'POST' });

            // Validate the REAL parsed graph (YOLO + Anthropic output) when we
            // have one. Only fall back to the datacenter sample if nothing has
            // been parsed/loaded yet, so the button still works on first open.
            let graphToCheck = currentGraph;
            let usingSample = false;
            if (!graphToCheck) {
                usingSample = true;
                const sampleR = await fetch('/demo/compliance-graph');
                graphToCheck = sampleR.ok
                    ? await sampleR.json()
                    : await (await fetch('/demo-datacentre')).json();
            }

            if (subtitleEl) {
                subtitleEl.textContent = usingSample
                    ? 'TIA-942 / BEC — datacenter sample (parse a plan to check yours)'
                    : 'TIA-942 / BEC — validated on the current graph';
            }

            const vR = await fetch('/compliance/validate', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ graph: graphToCheck }),
            });

            if (vR.ok) {
                renderComplianceCards(await vR.json());
                return;
            }
            throw new Error(await vR.text());
        } catch (err) {
            console.warn('Live compliance validate failed, falling back to demo report:', err);
        }

        // Last-resort offline fallback: precomputed report.
        try {
            const r = await fetch('/demo/compliance-report');
            if (r.ok) {
                if (subtitleEl) {
                    subtitleEl.textContent = 'TIA-942 / BEC — offline sample report';
                }
                renderComplianceCards(await r.json());
            } else {
                throw new Error('No compliance data');
            }
        } catch (err) {
            complianceCards.innerHTML = `<p class="compliance-error">Compliance check failed: ${err.message}</p>`;
        }
    }

    function renderComplianceCards(report) {
        const statusEl = document.getElementById('compliance-status');
        statusEl.textContent = report.passed ? 'COMPLIANT' : 'NON-COMPLIANT';
        statusEl.className = 'status-badge ' + (report.passed ? 'success' : 'danger');

        complianceCards.innerHTML = '';
        const violations = report.violations || [];
        const checksRun = report.checks_run || 0;
        const passedCount = checksRun - violations.length;

        if (passedCount > 0) {
            const passCard = document.createElement('div');
            passCard.className = 'compliance-card pass';
            passCard.innerHTML = `<strong>${passedCount} rules passed</strong><p>Geometry meets TIA-942 requirements.</p>`;
            complianceCards.appendChild(passCard);
        }

        violations.forEach(v => {
            const card = document.createElement('div');
            card.className = 'compliance-card fail';
            const rule = v.rule || {};
            card.innerHTML = `
                <strong>${esc(rule.condition || 'violation')} — ${esc(rule.target_class || '')}/${esc(rule.target_type || '')}</strong>
                <p>${esc(v.message || '')}</p>
                ${v.geometry_id ? `<span class="compliance-id">Object: ${esc(v.geometry_id)}</span>` : ''}
                ${rule.description ? `<span class="compliance-desc">${esc(rule.description)}</span>` : ''}`;
            complianceCards.appendChild(card);
        });

        if (!violations.length && checksRun > 0) {
            const card = document.createElement('div');
            card.className = 'compliance-card pass';
            card.innerHTML = '<strong>All checks passed</strong>';
            complianceCards.appendChild(card);
        }
    }

    async function applyGraph(graph, file, overlayUrl, demo = false) {
        currentGraph = graph;
        currentFile = file;
        isDemoMode = demo;
        demoOverlayUrl = overlayUrl;

        // Hand the parsed graph to the 3D twin page (same-origin localStorage).
        try { localStorage.setItem('flowdraft:graph', JSON.stringify(graph)); } catch (e) {}

        updateSummary(graph, demo);
        showJson(graph);
        renderRooms(graph);
        await loadEstates(financeDistrict.value);
        await updateFinance(graph);

        // Pre-baked demo PNG only for explicit "Load demo". Live uploads always
        // render via /overlay + visualize.py on the uploaded file + parsed graph.
        if (demo && overlayUrl) {
            showOverlay(overlayUrl);
        } else if (file) {
            const url = await renderOverlayFromApi(file, graph);
            showOverlay(url);
        }

        switchTab('overlay-view');
    }

    loadDemoBtn.addEventListener('click', async () => {
        setLoading(true);
        try {
            const r = await fetch('/demo/floorplan');
            if (!r.ok) throw new Error(await r.text());
            const data = await r.json();
            uploadZone.classList.add('has-file');
            document.querySelector('.upload-text').textContent = 'Demo: F2 floor plan';
            processBtn.disabled = false;
            await applyGraph(data.graph, null, data.overlay_url, true);
            toast('Demo floor plan loaded — offline ready', 'success');
        } catch (err) {
            toast('Demo load failed: ' + err.message, 'error');
        } finally {
            setLoading(false);
        }
    });

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (!fileInput.files.length && !isDemoMode) return;

        setLoading(true);
        switchTab('overlay-view');
        startParseProgress();
        const formData = new FormData(form);
        const engine = formData.get('engine');
        const diagram_type = formData.get('diagram_type');
        const conf = formData.get('conf');
        const handwriting = formData.get('handwriting') === 'on' ? 'true' : 'false';

        const file = fileInput.files[0];
        const url = `/parse?diagram_type=${diagram_type}&conf=${conf}&engine=${engine}&handwriting=${handwriting}`;

        try {
            const response = await fetch(url, { method: 'POST', body: formData });
            if (!response.ok) {
                const errText = await response.text();
                throw new Error(parseApiError(errText));
            }
            const graph = await response.json();
            stopParseProgress();

            try {
                await applyGraph(graph, file, null, false);
                toast(`Parsed ${(graph.spaces || []).length} rooms — overlay rendered`, 'success');
            } catch (overlayErr) {
                toast('Overlay render failed: ' + overlayErr.message, 'error');
                if (file) showUploadPreview(file);
            }
        } catch (parseErr) {
            toast('Parse failed: ' + (parseErr.message || parseErr), 'error');
            if (file) showUploadPreview(file);
        } finally {
            stopParseProgress();
            setLoading(false);
        }
    });

    financeDistrict.addEventListener('change', async () => {
        await loadEstates(financeDistrict.value);
        if (currentGraph) await fetchPropertyFinance(currentGraph);
    });
    financeEstate.addEventListener('change', () => {
        if (currentGraph) fetchPropertyFinance(currentGraph);
    });
    financeRecalc.addEventListener('click', () => {
        if (currentGraph) fetchPropertyFinance(currentGraph);
    });
    runComplianceBtn.addEventListener('click', runCompliance);

    copyBtn.addEventListener('click', () => {
        navigator.clipboard.writeText(jsonOutput.textContent);
        copyBtn.textContent = 'Copied!';
        toast('Graph JSON copied to clipboard', 'success');
        setTimeout(() => { copyBtn.textContent = 'Copy JSON'; }, 2000);
    });

    if (resultImage && lightbox) {
        resultImage.addEventListener('click', () => {
            if (!resultImage.src) { return; }
            lightboxImg.src = resultImage.src;
            lightbox.classList.remove('hidden');
        });
        const closeLightbox = () => lightbox.classList.add('hidden');
        lightbox.addEventListener('click', (e) => {
            if (e.target === lightbox || e.target === lightboxClose) { closeLightbox(); }
        });
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && !lightbox.classList.contains('hidden')) { closeLightbox(); }
        });
    }

    loadEstates('Kowloon');
});
