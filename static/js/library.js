(function () {
  // карусель: 4 карточки в видимой области, остальные — стрелками
  document.querySelectorAll('[data-carousel]').forEach(function (root) {
    if (root.dataset.carouselInit === '1') {
      return;
    }
    root.dataset.carouselInit = '1';
    const track = root.querySelector('.grid');
    const prev = root.querySelector('.car-prev');
    const next = root.querySelector('.car-next');
    if (!track || !prev || !next) {
      return;
    }
    const step = function () {
      const card = track.querySelector('.card');
      if (!card) {
        return track.clientWidth;
      }
      const gap = parseFloat(getComputedStyle(track).columnGap || '16') || 16;
      return card.getBoundingClientRect().width + gap;
    };
    const update = function () {
      const max = track.scrollWidth - track.clientWidth - 2;
      prev.disabled = track.scrollLeft <= 2;
      next.disabled = max <= 0 || track.scrollLeft >= max;
    };
    prev.addEventListener('click', function () {
      track.scrollBy({ left: -step() * 2, behavior: 'smooth' });
    });
    next.addEventListener('click', function () {
      track.scrollBy({ left: step() * 2, behavior: 'smooth' });
    });
    track.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    update();
  });

  const modal = document.getElementById('play-modal');
  if (!modal) {
    return;
  }

  const host = document.getElementById('modal-dos');
  const startScreen = document.getElementById('modal-start');
  const startButton = document.getElementById('modal-start-btn');
  const startHint = document.getElementById('modal-start-hint');
  const statusEl = document.getElementById('modal-status');
  const titleEl = document.getElementById('modal-title');
  const openTab = document.getElementById('modal-open-tab');

  const jsDosSrc = modal.dataset.jsdos;
  const jsDosCss = modal.dataset.jsdosCss;
  const emulators = modal.dataset.emulators;

  let current = null;
  let player = null;
  let jsDosPromise = null;

  function report(message, where) {
    try {
      fetch('/api/client-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: String(message).slice(0, 600), where: where || 'modal' })
      }).catch(function () {});
    } catch (error) {
      /* журнал не должен ломать плеер */
    }
  }

  function say(text, kind) {
    statusEl.textContent = text;
    statusEl.className = 'player-status' + (kind ? ' ' + kind : '');
    console.log('[retro]', text);
    if (kind === 'err') {
      report(text, 'status');
    }
  }

  function loadJsDos() {
    if (window.Dos) {
      return Promise.resolve();
    }
    if (jsDosPromise) {
      return jsDosPromise;
    }
    jsDosPromise = new Promise(function (resolve, reject) {
      if (jsDosCss && !document.querySelector('link[data-jsdos-css]')) {
        const link = document.createElement('link');
        link.rel = 'stylesheet';
        link.href = jsDosCss;
        link.dataset.jsdosCss = '1';
        document.head.appendChild(link);
      }
      const script = document.createElement('script');
      script.src = jsDosSrc;
      script.onload = function () {
        if (typeof window.Dos === 'function') {
          resolve();
        } else {
          reject(new Error('js-dos загрузился, но Dos не найден'));
        }
      };
      script.onerror = function () {
        reject(new Error('не удалось скачать js-dos'));
      };
      document.head.appendChild(script);
    });
    return jsDosPromise;
  }

  function resetStage() {
    if (player) {
      try {
        player.stop();
      } catch (error) {
        console.warn('[retro] stop failed', error);
        report(error, 'stop');
      }
      player = null;
    }
    window.__retroPlayer = null;
    host.innerHTML = '';
    if (startScreen) {
      startScreen.hidden = false;
      host.appendChild(startScreen);
    }
    if (startButton) {
      startButton.disabled = false;
      startButton.textContent = '▶ Запустить игру';
    }
    if (startHint) {
      startHint.textContent = 'Эмулятор загрузится по нажатию';
    }
  }

  function openModal(link) {
    current = {
      title: link.getAttribute('data-title') || 'Игра',
      bundle: link.getAttribute('data-bundle'),
      page: link.getAttribute('href'),
      gameId: link.getAttribute('data-game'),
      backend: link.getAttribute('data-backend') || 'dosbox'
    };
    titleEl.textContent = current.title;
    openTab.href = current.page;
    resetStage();
    say('Готов к запуску');
    modal.hidden = false;
    document.body.classList.add('modal-open');
    startButton.focus();
  }

  function closeModal() {
    modal.hidden = true;
    document.body.classList.remove('modal-open');
    resetStage();
    current = null;
  }

  function startGame() {
    if (!current || player) {
      return;
    }
    if (current.gameId) {
      fetch('/api/play/' + encodeURIComponent(current.gameId), { method: 'POST' }).catch(function () {});
    }
    startButton.disabled = true;
    startButton.textContent = 'Запускаем…';
    say('Загружаем эмулятор…');

    loadJsDos()
      .then(function () {
        say('Создаём эмулятор…');
        host.innerHTML = '';
        try {
          player = window.Dos(host, {
            url: current.bundle,
            pathPrefix: emulators,
            theme: 'dark',
            lang: 'ru',
            backend: current.backend || 'dosbox',
            backendLocked: true,
            workerThread: false,
            autoStart: true,
            autoSave: !!window.__jsdosStorageOk,
            mouseCapture: false,
            mobileControls: true,
            imageRendering: 'pixelated',
            renderAspect: '4/3',
            onEvent: function (event) {
              console.log('[retro] js-dos event:', event);
              say('js-dos: ' + event);
              if (event === 'emu-ready' || event === 'ci-ready' || event === 'bundle-ready') {
                say('Игра запущена', 'ok');
              }
            }
          });
          window.__retroPlayer = player;
          setTimeout(function () {
            var canvas = host.querySelector('canvas');
            if (canvas) {
              canvas.tabIndex = 0;
              canvas.focus();
            }
          }, 600);
        } catch (error) {
          say('Не удалось создать плеер: ' + error, 'err');
          report(error, 'create');
          startButton.disabled = false;
          startButton.textContent = '↻ Попробовать снова';
          return;
        }

        setTimeout(function () {
          if (!host.querySelector('canvas')) {
            say('Эмулятор не показал экран за 20 секунд — откройте F12 и пришлите ошибки', 'err');
          }
        }, 20000);
      })
      .catch(function (error) {
        say(String(error.message || error), 'err');
        startButton.disabled = false;
        startButton.textContent = '↻ Попробовать снова';
      });
  }

  document.querySelectorAll('[data-play]').forEach(function (link) {
    link.addEventListener('click', function (event) {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
        return;
      }
      event.preventDefault();
      openModal(link);
    });
  });

  modal.querySelectorAll('[data-modal-close]').forEach(function (element) {
    element.addEventListener('click', closeModal);
  });

  startButton.addEventListener('click', startGame);

  document.addEventListener('keydown', function (event) {
    // Esc закрывает окно только до запуска: во время игры он нужен эмулятору.
    if (event.key === 'Escape' && !modal.hidden && !player) {
      closeModal();
    }
  });

  window.addEventListener('unhandledrejection', function (event) {
    if (modal.hidden) {
      return;
    }
    const reason = event.reason && (event.reason.message || event.reason);
    console.warn('[retro] unhandled rejection:', reason);
    report(reason, 'unhandledrejection');
    say('Ошибка эмулятора: ' + reason, 'err');
  });
})();

