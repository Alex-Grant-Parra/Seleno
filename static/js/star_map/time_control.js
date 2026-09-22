// Star map - the date/time input: segment detection and arrow-key rollover.

// Time and sidereal time utilities
// Date/time helpers for local datetime input handling with rollover
function formatLocalDateTime(date) {
    // Formats a Date as YYYY-MM-DDTHH:MM in local time
    const pad = (n) => n.toString().padStart(2, '0');
    return `${date.getFullYear()}-${pad(date.getMonth()+1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function parseLocalDateTime(str) {
    // Safely parse local datetime string (YYYY-MM-DDTHH:MM)
    // new Date(str) with no timezone is treated as local time by modern browsers
    const d = new Date(str);
    if (isNaN(d.getTime())) return null;
    return d;
}

function setTimeControlFromDate(d, preserveSelectionSegment) {
    if (!timeControl || !(d instanceof Date) || isNaN(d)) return;
    const segBounds = preserveSelectionSegment ? getCurrentSegmentBounds(timeControl) : null;
    timeControl.value = formatLocalDateTime(d);
    // Try to keep the caret on the same segment if supported
    if (segBounds && typeof timeControl.setSelectionRange === 'function') {
        try { timeControl.setSelectionRange(segBounds.start, segBounds.end); } catch {}
    }
}

// Determine which segment of the datetime string the caret is on
// Returns one of: 'year'|'month'|'day'|'hour'|'minute' or null if unknown
function getCaretSegment(input) {
    // For datetime-local inputs, selectionStart is unreliable, so we return null
    // and rely on virtualTimeSegment set by click handlers
    return null;
}

// Virtual caret segment used when browser doesn't expose selectionStart for datetime-local
let virtualTimeSegment = null;

// Map a click position within the input to an approximate character index and then segment
function getSegmentFromIndex(index, value) {
    if (!value) return null;
    const re = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/;
    const m = value.match(re);
    if (!m) return null;
    let idx = 0;
    const ranges = {};
    ranges.year = { start: idx, end: idx + m[1].length - 1 };
    idx += m[1].length; idx += 1; // '-'
    ranges.month = { start: idx, end: idx + m[2].length - 1 };
    idx += m[2].length; idx += 1; // '-'
    ranges.day = { start: idx, end: idx + m[3].length - 1 };
    idx += m[3].length; idx += 1; // 'T'
    ranges.hour = { start: idx, end: idx + m[4].length - 1 };
    idx += m[4].length; idx += 1; // ':'
    ranges.minute = { start: idx, end: idx + m[5].length - 1 };
    idx += m[5].length;
    if (m[6]) { idx += 1; ranges.second = { start: idx, end: idx + m[6].length - 1 }; }

    for (const seg of ['year','month','day','hour','minute','second']) {
        if (!ranges[seg]) continue;
        if (index >= ranges[seg].start && index <= ranges[seg].end + 1) return seg;
    }
    return null;
}

function handleTimeInputClick(e) {
    const input = e.currentTarget;
    const value = input.value || '';
    if (!value) return;
    
    // For datetime-local inputs, estimate segment based on click position
    const rect = input.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    
    // Create a temporary span to measure the actual text width
    const tempSpan = document.createElement('span');
    tempSpan.style.cssText = `
        position: absolute; 
        visibility: hidden; 
        white-space: nowrap;
        font-family: ${getComputedStyle(input).fontFamily};
        font-size: ${getComputedStyle(input).fontSize};
        font-weight: ${getComputedStyle(input).fontWeight};
    `;
    
    // Format the value for display (DD/MM/YYYY HH:MM)
    const dateObj = new Date(value);
    const displayText = dateObj.toLocaleString('en-GB', { 
        day: '2-digit', 
        month: '2-digit', 
        year: 'numeric', 
        hour: '2-digit', 
        minute: '2-digit',
        hour12: false 
    }).replace(',', '');
    
    tempSpan.textContent = displayText;
    document.body.appendChild(tempSpan);
    const textWidth = tempSpan.offsetWidth;
    document.body.removeChild(tempSpan);
    
    // Calculate padding to center the text in the input
    const padding = (rect.width - textWidth) / 2;
    const textStart = padding;
    const textEnd = padding + textWidth;
    
    // Check if click is within the text area
    if (clickX < textStart || clickX > textEnd) {
        // Click is in padding area, ignore or default to last segment
        console.log(`Click at ${clickX.toFixed(0)}px is outside text area (${textStart.toFixed(0)}-${textEnd.toFixed(0)}px)`);
        return;
    }
    
    // Calculate relative position within the actual text
    const relativePosition = (clickX - textStart) / textWidth;
    console.log(`Click at ${(relativePosition * 100).toFixed(1)}% of text width`);
    
    let seg = null;
    
    // Format is DD/MM/YYYY HH:MM (UK/ISO format)
    // Split based on typical character positions:
    // DD (2) / (1) MM (2) / (1) YYYY (4) space (1) HH (2) : (1) MM (2) = 18 chars
    // Proportions: day=2/18, month=2/18, year=4/18, hour=2/18, minute=2/18
    
    if (relativePosition < 0.15) {
        seg = 'day';       // First ~15% (DD)
    } else if (relativePosition < 0.3) {
        seg = 'month';     // Next ~15% (MM)
    } else if (relativePosition < 0.55) {
        seg = 'year';      // Next ~25% (YYYY)
    } else if (relativePosition < 0.75) {
        seg = 'hour';      // Next ~20% (HH)
    } else {
        seg = 'minute';    // Last ~25% (MM)
    }
    
    console.log(`Detected segment: ${seg}`);
    
    if (seg) {
        virtualTimeSegment = seg;
        updateTimeSegmentIndicator(seg);
    }
}

function getCurrentSegmentBounds(input) {
    const v = input.value || '';
    let seg = getCaretSegment(input);
    
    // If no caret segment detected, use virtualTimeSegment
    if (!seg && virtualTimeSegment) {
        seg = virtualTimeSegment;
    }
    
    if (!seg) return null;

    // Recompute ranges using the same regex-based method so bounds match caret detection
    const re = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/;
    const m = v.match(re);
    if (!m) return null;

    let idx = 0;
    const bounds = {};
    bounds.year = { start: idx, end: idx + m[1].length };
    idx += m[1].length; idx += 1; // '-'
    bounds.month = { start: idx, end: idx + m[2].length };
    idx += m[2].length; idx += 1; // '-'
    bounds.day = { start: idx, end: idx + m[3].length };
    idx += m[3].length; idx += 1; // 'T'
    bounds.hour = { start: idx, end: idx + m[4].length };
    idx += m[4].length; idx += 1; // ':'
    bounds.minute = { start: idx, end: idx + m[5].length };
    idx += m[5].length;
    if (m[6]) { idx += 1; bounds.second = { start: idx, end: idx + m[6].length }; }

    return bounds[seg] || null;
}

function adjustDateByUnit(date, unit, delta) {
    // Returns a new Date adjusted in local time, relying on JS rollover behavior
    const d = new Date(date.getTime());
    switch (unit) {
        case 'minute': d.setMinutes(d.getMinutes() + delta); break;
        case 'hour': d.setHours(d.getHours() + delta); break;
        case 'day': d.setDate(d.getDate() + delta); break;
        case 'month': d.setMonth(d.getMonth() + delta); break;
        case 'year': d.setFullYear(d.getFullYear() + delta); break;
        case 'second': d.setSeconds(d.getSeconds() + delta); break;
        default: d.setMinutes(d.getMinutes() + delta); break;
    }
    // Zero seconds and ms for stability with our control
    if (unit !== 'second') d.setSeconds(0, 0);
    return d;
}

// Update the visual indicator showing which time segment is selected
let segmentIndicatorTimeout = null;
function updateTimeSegmentIndicator(segment) {
    if (!timeSegmentIndicator) return;
    
    const labels = {
        'year': 'Year',
        'month': 'Month',
        'day': 'Day',
        'hour': 'Hour',
        'minute': 'Minute'
    };
    
    if (segment && labels[segment]) {
        timeSegmentIndicator.textContent = `[${labels[segment]}]`;
        timeSegmentIndicator.style.display = 'inline';
        
        // Auto-hide after 2 seconds
        if (segmentIndicatorTimeout) clearTimeout(segmentIndicatorTimeout);
        segmentIndicatorTimeout = setTimeout(() => {
            timeSegmentIndicator.style.display = 'none';
        }, 2000);
    } else {
        timeSegmentIndicator.style.display = 'none';
    }
}

function handleTimeControlKeydown(e) {
    if (!timeControl) return;
    const key = e.key;
    
    // Handle Tab key to cycle through segments
    if (key === 'Tab') {
        e.preventDefault();
        const segments = ['hour', 'minute', 'day', 'month', 'year'];
        const currentIndex = segments.indexOf(virtualTimeSegment || 'hour');
        const nextIndex = e.shiftKey 
            ? (currentIndex - 1 + segments.length) % segments.length 
            : (currentIndex + 1) % segments.length;
        virtualTimeSegment = segments[nextIndex];
        updateTimeSegmentIndicator(virtualTimeSegment);
        return;
    }
    
    if (key !== 'ArrowUp' && key !== 'ArrowDown') return;

    const raw = timeControl.value;
    let baseDate = parseLocalDateTime(raw);
    if (!baseDate) {
        baseDate = new Date();
        baseDate.setSeconds(0, 0);
    }

    // Try multiple methods to determine which segment is focused
    let unit = null;
    
    // Method 1: Try to get caret position (works in some browsers)
    unit = getCaretSegment(timeControl);
    
    // Method 2: Use stored virtual segment from last click
    if (!unit && virtualTimeSegment) {
        unit = virtualTimeSegment;
    }
    
    // Method 3: Use modifier keys for explicit control
    if (!unit || e.ctrlKey || e.shiftKey || e.altKey) {
        if (e.ctrlKey && e.shiftKey) unit = 'year';
        else if (e.altKey) unit = 'month';
        else if (e.ctrlKey) unit = 'day';
        else if (e.shiftKey) unit = 'minute';
    }
    
    // Method 4: If still no unit and no virtualTimeSegment, start with hour as default
    if (!unit) {
        unit = 'hour';
        virtualTimeSegment = 'hour';
    }
    
    // Prevent native browser handling to enable our rollover behavior
    e.preventDefault();
    e.stopPropagation();
    
    // Store the unit for consistency in subsequent keypresses
    virtualTimeSegment = unit;
    
    // Show visual feedback of which segment is being adjusted
    updateTimeSegmentIndicator(unit);

    const delta = (key === 'ArrowUp') ? 1 : -1;
    const newDate = adjustDateByUnit(baseDate, unit, delta);

    // Update input and redraw
    setTimeControlFromDate(newDate, true);
    schedulePlanetsRefresh();
    draw();
}
