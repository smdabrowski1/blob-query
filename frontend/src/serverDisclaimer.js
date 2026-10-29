// Shared by QueryEditor.jsx (Ask AI tab) and App.jsx (discovery chat landing
// screen) — same sessionStorage key, so dismissing the banner in either
// place dismisses it in both, since it's the same underlying warning.
export const SERVER_DISCLAIMER_KEY = "blobquery-server-disclaimer-dismissed";

export function isServerDisclaimerDismissed() {
  return sessionStorage.getItem(SERVER_DISCLAIMER_KEY) === "1";
}

export function dismissServerDisclaimer() {
  sessionStorage.setItem(SERVER_DISCLAIMER_KEY, "1");
}
