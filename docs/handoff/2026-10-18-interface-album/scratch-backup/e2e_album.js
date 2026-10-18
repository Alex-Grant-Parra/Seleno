(async () => {
    const R = { errors: [], checks: {} };
    window.addEventListener('error', e => R.errors.push(`error: ${e.message} @ ${e.filename}:${e.lineno}`));
    const origError = console.error;
    console.error = (...a) => { R.errors.push('console.error: ' + a.map(String).join(' ')); origError(...a); };
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const until = async (fn, ms = 6000) => { const t = Date.now(); while (Date.now() - t < ms) { try { if (fn()) return true; } catch (e) {} await sleep(100); } return false; };
    try {
        R.checks.byDate = await until(() => document.querySelectorAll('#gallery .photo-card').length === 3);
        R.checks.subtitle = [...document.querySelectorAll('#gallery .photo-card .text-muted')].map(e => e.textContent);
        document.getElementById('frame-type-filter').value = 'light';
        document.getElementById('frame-type-filter').dispatchEvent(new Event('change'));
        R.checks.filtered = await until(() => document.querySelectorAll('#gallery .photo-card').length === 1);
        document.getElementById('frame-type-filter').value = '';
        document.getElementById('view-by-plan').click();
        R.checks.planView = await until(() => document.querySelector('#gallery section h4') !== null);
        R.checks.planText = document.getElementById('gallery').innerText;
        R.checks.zipLink = document.querySelector('#gallery section a').getAttribute('href');
        document.querySelector('#gallery .photo-card').click();
        R.checks.preview = await until(() => document.getElementById('preview-title').textContent.endsWith('.JPG'));
    } catch (e) { R.errors.push('exception: ' + e + ' ' + e.stack); }
    await fetch('/e2e-result', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(R) });
})();
