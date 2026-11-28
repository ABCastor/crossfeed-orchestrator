// One source for colour, size and motion. panel.css reads the palette as CSS variables;
// the token sheets (Aplus, Bplus) copy these values literally for SwiftUI.
window.TOKENS = {
  palette: {
    light: {
      paper: "#FAF9F4", sunk: "#EFEDE6", tile: "#FFFFFF",
      ink: "#14130F", quiet: "#5E594E", faint: "#8A8478",
      rule: "#E2DDD2", tileEdge: "rgba(20,19,15,0.07)",
      track: "#E6E2D8",
      teal: "#006770", tealText: "#006770", tealWash: "rgba(0,103,112,0.11)",
      terra: "#C4552A", terraText: "#A2441F",
      olive: "#667D2F", oliveText: "#4F6322",
      spare: "#A3BD5E", hatch: "rgba(255,255,255,0.34)",
      marker: "#14130F",
      button: "#EAE7DE", buttonHover: "#E0DCD1", buttonText: "#14130F",
      primary: "#006770", primaryText: "#FFFFFF",
      confirm: "#F1EEE6",
      segment: "#EAE7DE", segmentOn: "#FFFFFF",
      hover: "rgba(20,19,15,0.06)", grip: "#8A8478",
      spark: "#C9C3B6", sparkToday: "#006770",
      liftShadow: "0 4px 12px rgba(20,19,15,0.08), 0 24px 64px rgba(20,19,15,0.16)",
    },
    dark: {
      paper: "#1B1A17", sunk: "#131210", tile: "#22211D",
      ink: "#ECE9E2", quiet: "#B4AFA6", faint: "#8C877E",
      rule: "#36332D", tileEdge: "rgba(236,233,226,0.06)",
      track: "#3A3731",
      teal: "#00858F", tealText: "#5DB3BA", tealWash: "rgba(72,162,170,0.16)",
      terra: "#D9693D", terraText: "#EC9672",
      olive: "#8FA84F", oliveText: "#B3C97A",
      spare: "#7D9545", hatch: "rgba(27,26,23,0.30)",
      marker: "#ECE9E2",
      button: "#34322D", buttonHover: "#3E3B35", buttonText: "#ECE9E2",
      primary: "#00858F", primaryText: "#FFFFFF",
      confirm: "#2C2A25",
      segment: "#2C2A26", segmentOn: "#4A4740",
      hover: "rgba(236,233,226,0.07)", grip: "#8C877E",
      spark: "#4A4740", sparkToday: "#5DB3BA",
      liftShadow: "0 4px 12px rgba(0,0,0,0.5), 0 24px 64px rgba(0,0,0,0.6)",
    },
  },
  // Motion: DRAG.md section B (family layer, shared with Pack and Read Later). Do not tune per app.
  motion: {
    slop: 7,                       // pt of travel before a press becomes a drag (mouse, trackpad)
    liftScale: 1.02,               // carried block or tab
    liftSpring: { stiffness: 420, damping: 28, mass: 1, settleMs: 510 }, // --spring-settle, damping ratio 0.68
    settleMs: 200,                 // --t-drag-settle: release and cancel
    settleEase: [0.2, 0.8, 0.2, 1],// --ease
    reducedMs: 150,                // reduced motion: settle and cancel, same path, no lift scale
    keyMoveMs: 200,                // a key or menu move uses the same settle
    stepBack: 0.38,                // illegal places while carrying (none here: every order is legal)
  },
  rates: { GBP: 1.27, EUR: 1.08, USD: 1 },
};

// The palette reaches panel.css as CSS variables, generated here so colour has one source
// (theme.js) for the web panel. The page's content policy allows inline styles for the panel's
// own bars, so a <style> element is available without widening anything else.
(function () {
  const css = Object.entries(TOKENS.palette)
    .map(([theme, palette]) => `.panel.${theme},body.panelpage.${theme},.sheet.${theme}{${Object.entries(palette).map(([key, value]) => `--${key}:${value}`).join(";")}}`)
    .join("\n");
  const style = document.createElement("style");
  style.textContent = css;
  document.head.appendChild(style);
})();
