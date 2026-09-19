/**
 * 数字炸弹前端逻辑
 *
 * 界面完全由服务端下发的 phase 驱动，客户端不做任何规则判定（炸弹数字在游戏
 * 过程中不会下发）。本地只保留输入格式的前置提示，最终合法性仍以服务端为准。
 */

(function () {
  "use strict";

  // --- DOM 引用 ------------------------------------------------------------
  const el = {
    connStatus: document.getElementById("connStatus"),
    screens: {
      idle: document.getElementById("screen-idle"),
      lobby: document.getElementById("screen-lobby"),
      playing: document.getElementById("screen-playing"),
      boom: document.getElementById("screen-boom"),
    },
    nicknameInput: document.getElementById("nicknameInput"),
    joinBtn: document.getElementById("joinBtn"),
    lobbyCount: document.getElementById("lobbyCount"),
    lobbyPlayers: document.getElementById("lobbyPlayers"),
    startBtn: document.getElementById("startBtn"),
    closeBtn: document.getElementById("closeBtn"),
    rangeText: document.getElementById("rangeText"),
    turnTip: document.getElementById("turnTip"),
    guessField: document.getElementById("guessField"),
    guessLabel: document.getElementById("guessLabel"),
    guessInput: document.getElementById("guessInput"),
    guessBtn: document.getElementById("guessBtn"),
    turnCount: document.getElementById("turnCount"),
    lastGuessLine: document.getElementById("lastGuessLine"),
    gamePlayers: document.getElementById("gamePlayers"),
    closeInGameBtn: document.getElementById("closeInGameBtn"),
    boomNickname: document.getElementById("boomNickname"),
    boomNumber: document.getElementById("boomNumber"),
    boomCountdown: document.getElementById("boomCountdown"),
    toastHost: document.getElementById("toastHost"),
  };

  // --- 连接状态 ------------------------------------------------------------
  let ws = null;
  let myId = null;
  let reconnectDelay = 1000;
  let manualClose = false;
  let heartbeatTimer = null;
  let countdownTimer = null;
  let currentPhase = null;
  /** 记录已加入过的昵称，重连后自动尝试回到房间 */
  let lastNickname = null;

  function wsUrl() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${location.host}/ws`;
  }

  function connect() {
    if (manualClose) return;
    try {
      ws = new WebSocket(wsUrl());
    } catch (err) {
      setConn("offline", "连接失败");
      scheduleReconnect();
      return;
    }

    ws.onopen = function () {
      reconnectDelay = 1000;
      setConn("online", "已连接");
      startHeartbeat();
      // 重连后若曾在大厅，尝试自动归队
      if (lastNickname) {
        send({ action: "join_lobby", nickname: lastNickname });
      }
    };

    ws.onmessage = function (event) {
      let data;
      try {
        data = JSON.parse(event.data);
      } catch (err) {
        return;
      }
      handleMessage(data);
    };

    ws.onclose = function () {
      stopHeartbeat();
      setConn("offline", "已断开，重连中…");
      scheduleReconnect();
    };

    ws.onerror = function () {
      setConn("offline", "连接异常");
    };
  }

  function scheduleReconnect() {
    if (manualClose) return;
    setTimeout(connect, reconnectDelay);
    // 指数退避，上限 8 秒
    reconnectDelay = Math.min(reconnectDelay * 1.6, 8000);
  }

  function startHeartbeat() {
    stopHeartbeat();
    heartbeatTimer = setInterval(function () {
      send({ action: "ping" });
    }, 25000);
  }

  function stopHeartbeat() {
    if (heartbeatTimer) {
      clearInterval(heartbeatTimer);
      heartbeatTimer = null;
    }
  }

  function setConn(state, text) {
    el.connStatus.className = "conn " + state;
    el.connStatus.textContent = text;
  }

  function send(payload) {
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      toast("连接未就绪，请稍后重试", "error");
      return false;
    }
    ws.send(JSON.stringify(payload));
    return true;
  }

  // --- 消息处理 ------------------------------------------------------------
  function handleMessage(data) {
    switch (data.type) {
      case "welcome":
        myId = data.playerId;
        break;
      case "state":
        render(data);
        break;
      case "boom":
        showBoom(data);
        break;
      case "error":
        toast(data.message, "error");
        if (data.code === "OUT_OF_RANGE" || data.code === "INVALID_NUMBER") {
          el.guessInput.focus();
          el.guessInput.select();
        }
        break;
      case "pong":
        break;
      default:
        break;
    }
  }

  // --- 渲染 ----------------------------------------------------------------
  function render(state) {
    currentPhase = state.phase;

    // 未加入房间的玩家始终显示主界面，不受全局 phase 影响
    var displayPhase = state.you && state.you.inRoom ? state.phase : "idle";
    showScreen(displayPhase);

    switch (displayPhase) {
      case "idle":
        renderIdle(state);
        break;
      case "lobby":
        renderLobby(state);
        break;
      case "playing":
        renderPlaying(state);
        break;
      case "boom":
        // Boom 界面由 boom 消息驱动展示，state 仅同步房间信息
        renderBoomState(state);
        break;
      default:
        break;
    }
  }

  function showScreen(phase) {
    Object.keys(el.screens).forEach(function (key) {
      el.screens[key].hidden = key !== phase;
    });
    // 离开 Boom 界面时清理倒计时
    if (phase !== "boom" && countdownTimer) {
      clearInterval(countdownTimer);
      countdownTimer = null;
    }
  }

  function renderIdle(state) {
    if (!el.nicknameInput.value && state.you && state.you.nickname) {
      el.nicknameInput.value = state.you.nickname;
    }
    setJoinable(ws && ws.readyState === WebSocket.OPEN);
    el.nicknameInput.focus();
  }

  function renderLobby(state) {
    const players = (state.room && state.room.players) || [];
    el.lobbyCount.textContent = players.length;
    renderPlayerList(el.lobbyPlayers, players, null);
    setJoinable(true);
  }

  function renderPlaying(state) {
    const game = state.game || {};
    const players = (state.room && state.room.players) || [];

    el.rangeText.textContent = `${game.left} - ${game.right}`;
    el.turnCount.textContent = game.turnCount || 0;

    if (game.lastGuess) {
      const label = game.lastGuess.result === "boom" ? "踩雷" : "安全";
      el.lastGuessLine.textContent =
        `上一次：${game.lastGuess.nickname} 猜 ${game.lastGuess.value}（${label}）`;
    } else {
      el.lastGuessLine.textContent = "本局尚无人猜测";
    }

    renderPlayerList(el.gamePlayers, players, game.currentPlayerId);

    if (game.isYourTurn) {
      el.turnTip.textContent = "轮到你了，请输入一个数字";
      el.turnTip.classList.add("yours");
      el.guessLabel.textContent = `请输入从 ${game.left} - ${game.right}（边界值不可猜）`;
      el.guessField.hidden = false;
      el.guessBtn.hidden = false;
      el.guessInput.min = game.left + 1;
      el.guessInput.max = game.right - 1;
      el.guessInput.value = "";
      el.guessInput.disabled = false;
      el.guessBtn.disabled = false;
      setTimeout(function () { el.guessInput.focus(); }, 60);
    } else {
      el.turnTip.textContent = `等待 ${game.currentPlayerNickname || "其他玩家"} 操作…`;
      el.turnTip.classList.remove("yours");
      el.guessField.hidden = true;
      el.guessBtn.hidden = true;
      el.guessInput.disabled = true;
    }
  }

  function renderPlayerList(host, players, currentPlayerId) {
    host.innerHTML = "";
    players.forEach(function (player, index) {
      const li = document.createElement("li");
      const isActive = currentPlayerId && player.id === currentPlayerId;
      if (isActive) li.className = "active";

      const badge = document.createElement("span");
      badge.className = "order-badge";
      badge.textContent = String(index + 1);

      const name = document.createElement("span");
      name.className = "pname";
      name.textContent = player.nickname;

      li.appendChild(badge);
      li.appendChild(name);

      const tags = [];
      if (player.isYou) tags.push(["tag you", "你"]);
      if (isActive) tags.push(["tag turn", "当前回合"]);
      if (player.online === false) tags.push(["tag off", "离线"]);
      tags.forEach(function (t) {
        const tag = document.createElement("span");
        tag.className = t[0];
        tag.textContent = t[1];
        li.appendChild(tag);
      });

      host.appendChild(li);
    });
  }

  function setJoinable(enabled) {
    el.joinBtn.disabled = !enabled;
    el.startBtn.disabled = !enabled;
    el.closeBtn.disabled = !enabled;
    el.closeInGameBtn.disabled = !enabled;
  }

  // --- Boom ----------------------------------------------------------------
  function showBoom(data) {
    showScreen("boom");
    el.boomNickname.textContent = data.nickname || "玩家";
    el.boomNumber.textContent = data.bomb != null ? String(data.bomb) : "-";

    const totalMs = data.backToLobbyInMs || 3000;
    let remain = Math.ceil(totalMs / 1000);
    el.boomCountdown.textContent = String(remain);

    if (countdownTimer) clearInterval(countdownTimer);
    countdownTimer = setInterval(function () {
      remain -= 1;
      if (remain <= 0) {
        clearInterval(countdownTimer);
        countdownTimer = null;
        el.boomCountdown.textContent = "0";
      } else {
        el.boomCountdown.textContent = String(remain);
      }
    }, 1000);
  }

  function renderBoomState(state) {
    const game = state.game || {};
    if (game.boomNickname) el.boomNickname.textContent = game.boomNickname;
    if (game.bomb != null) el.boomNumber.textContent = String(game.bomb);
  }

  // --- 提示 ----------------------------------------------------------------
  function toast(message, kind) {
    const box = document.createElement("div");
    box.className = "toast " + (kind || "info");
    box.textContent = message;
    el.toastHost.appendChild(box);
    setTimeout(function () {
      if (box.parentNode) box.parentNode.removeChild(box);
    }, 2600);
  }

  // --- 交互 ----------------------------------------------------------------
  function doJoin() {
    const nickname = (el.nicknameInput.value || "").trim();
    if (!nickname) {
      toast("请先输入微信昵称", "error");
      el.nicknameInput.focus();
      return;
    }
    lastNickname = nickname;
    send({ action: "join_lobby", nickname: nickname });
  }

  function doGuess() {
    const raw = el.guessInput.value;
    if (raw === "" || raw == null) {
      toast("请输入一个数字", "error");
      el.guessInput.focus();
      return;
    }
    // 前置格式检查：必须是整数。最终合法性（含范围）仍以服务端判定为准。
    const trimmed = String(raw).trim();
    if (!/^-?\d+$/.test(trimmed)) {
      toast("请输入一个整数", "error");
      el.guessInput.focus();
      return;
    }
    el.guessBtn.disabled = true;
    send({ action: "guess", value: Number(trimmed) });
  }

  el.joinBtn.addEventListener("click", doJoin);
  el.nicknameInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter") doJoin();
  });

  el.startBtn.addEventListener("click", function () {
    send({ action: "start_game" });
  });

  el.closeBtn.addEventListener("click", function () {
    lastNickname = null;
    send({ action: "close_lobby" });
  });

  el.closeInGameBtn.addEventListener("click", function () {
    lastNickname = null;
    send({ action: "close_lobby" });
  });

  el.guessBtn.addEventListener("click", doGuess);
  el.guessInput.addEventListener("keydown", function (e) {
    if (e.key === "Enter" && !el.guessBtn.disabled) doGuess();
  });

  // 页面关闭时主动断开，便于服务端及时清理房间
  window.addEventListener("beforeunload", function () {
    manualClose = true;
    if (ws) ws.close();
  });

  // --- 启动 ----------------------------------------------------------------
  setJoinable(false);
  showScreen("idle");
  connect();
})();
