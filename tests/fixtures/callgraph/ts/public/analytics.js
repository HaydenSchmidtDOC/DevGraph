// Classic script loaded before legacy.js; trackEvent is a global.
function trackEvent(name) {
  navigator.sendBeacon('/events', name);
}
