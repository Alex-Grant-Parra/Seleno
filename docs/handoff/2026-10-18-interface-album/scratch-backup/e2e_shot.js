// Screenshot mode: start live view and leave the page as a user would see it
(async () => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    for (let i = 0; i < 80 && selectedTelescopeId !== 'e2e-scope'; i++) await sleep(100);
    toggleLiveView();
    for (let i = 0; i < 100 && !liveViewPlayer.playingH264; i++) await sleep(100);
    await sleep(1500);
    document.title = 'READY';
    await fetch('/e2e-result', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ready: true }) });
})();
