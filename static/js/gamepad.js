/**
 * Навигация по сайту с геймпада (Gamepad API).
 *
 * D-pad / левый стик — перемещение фокуса, A — открыть, B — назад,
 * Start — переключить полноэкранный режим.
 * На странице игрока навигация отключена, чтобы не мешать игре.
 */
(function () {
  "use strict";

  const DEADZONE = 0.55;
  const REPEAT_MS = 180;

  const state = {
    index: -1,
    prev: {},
    axesPrev: { x: 0, y: 0 },
    lastMove: 0,
    id: null,
  };

  const isPlayer = () => document.body.classList.contains("is-player");

  function focusables() {
    return Array.from(
      document.querySelectorAll(
        "a[href], button:not([disabled]), input:not([type=hidden]):not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])"
      )
    ).filter((el) => el.offsetParent !== null && !el.closest(".jsdos, #dos"));
  }

  function highlight(list, index) {
    list.forEach((el) => el.classList.remove("gp-focus"));
    if (!list.length) return;
    state.index = ((index % list.length) + list.length) % list.length;
    const el = list[state.index];
    el.classList.add("gp-focus");
    el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function move(delta) {
    const list = focusables();
    if (!list.length) return;
    if (state.index < 0) {
      highlight(list, 0);
      return;
    }
    highlight(list, state.index + delta);
  }

  function activate() {
    const list = focusables();
    if (state.index < 0) {
      highlight(list, 0);
      return;
    }
    const el = list[state.index];
    if (!el) return;
    if (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT") {
      el.focus();
      return;
    }
    el.click();
  }

  function back() {
    if (isPlayer()) return;
    if (window.history.length > 1) window.history.back();
    else window.location.href = "/";
  }

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen?.();
  }

  function pressed(pad, button) {
    const value = pad.buttons[button] ? pad.buttons[button].value : 0;
    const now = value > 0.5;
    const was = !!state.prev[button];
    state.prev[button] = now;
    return now && !was;
  }

  function handle(pad) {
    if (isPlayer()) {
      // На странице игры не перехватываем кнопки — пусть работают как обычно.
      return;
    }

    const now = performance.now();
    const buttons = pad.buttons;

    const dpadUp = buttons[12] && buttons[12].pressed;
    const dpadDown = buttons[13] && buttons[13].pressed;
    const dpadLeft = buttons[14] && buttons[14].pressed;
    const dpadRight = buttons[15] && buttons[15].pressed;

    const axisX = pad.axes[0] || 0;
    const axisY = pad.axes[1] || 0;
    const stickUp = axisY < -DEADZONE;
    const stickDown = axisY > DEADZONE;
    const stickLeft = axisX < -DEADZONE;
    const stickRight = axisX > DEADZONE;

    if (now - state.lastMove > REPEAT_MS) {
      if (dpadUp || stickUp) { move(-1); state.lastMove = now; }
      else if (dpadDown || stickDown) { move(1); state.lastMove = now; }
      else if (dpadLeft || stickLeft) { move(-1); state.lastMove = now; }
      else if (dpadRight || stickRight) { move(1); state.lastMove = now; }
    }

    if (pressed(pad, 0)) activate();          // A
    if (pressed(pad, 1)) back();              // B
    if (pressed(pad, 8)) toggleFullscreen();  // Select
    if (pressed(pad, 9)) toggleFullscreen();  // Start

    state.axesPrev = { x: axisX, y: axisY };
  }

  function setConnected(connected) {
    document.body.classList.toggle("gp-connected", connected);
  }

  function poll() {
    let pad = null;
    if (navigator.getGamepads) {
      for (const candidate of navigator.getGamepads()) {
        if (candidate && candidate.connected) { pad = candidate; break; }
      }
    }
    setConnected(!!pad);
    if (pad) handle(pad);
    requestAnimationFrame(poll);
  }

  window.addEventListener("gamepadconnected", (event) => {
    console.log("[retro] геймпад подключён:", event.gamepad.id);
    setConnected(true);
  });

  window.addEventListener("gamepaddisconnected", () => setConnected(false));

  if (navigator.getGamepads) requestAnimationFrame(poll);
})();
