// Watch the selected telescope's live view, if live view is on
function updateLiveViewSrcForTelescope(telescopeId) {
    if (!telescopeId || !liveViewPlayer) return;
    if (liveViewActive) {
        liveViewPlayer.start(telescopeId);
    }
}

