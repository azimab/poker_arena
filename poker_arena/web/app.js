const SUITS = { s: "♠", h: "♥", d: "♦", c: "♣" };

function arena() {
  return {
    user: localStorage.getItem("username"),
    token: localStorage.getItem("token"),
    route: "leaderboard",
    params: [],
    data: null,
    error: "",
    busy: false,
    statusFilter: "",
    game: null,
    opponent: "builtin:call",
    raiseTo: 0,
    poll: null,

    get obs() {
      return this.game?.observation;
    },

    init() {
      const hash = new URLSearchParams(location.hash.slice(1));
      if (hash.has("access_token")) {
        this.setSession(hash.get("username"), hash.get("access_token"));
        history.replaceState(null, "", "#/");
      } else if (hash.has("error")) {
        this.error = hash.get("error");
        history.replaceState(null, "", "#/");
      }
      window.addEventListener("hashchange", () => this.navigate());
      if (this.token) this.api("/me").catch(() => {});
      this.navigate();
    },

    setSession(username, token) {
      this.user = username;
      this.token = token;
      if (token) {
        localStorage.setItem("username", username);
        localStorage.setItem("token", token);
      } else {
        localStorage.removeItem("username");
        localStorage.removeItem("token");
      }
    },

    async logout() {
      await this.api("/logout", { method: "POST" }).catch(() => {});
      this.setSession(null, null);
      location.hash = "#/";
    },

    async api(path, options = {}) {
      const headers = { ...options.headers };
      if (this.token) headers.Authorization = `Bearer ${this.token}`;
      const response = await fetch(path, { ...options, headers });
      if (response.status === 401 && this.token) this.setSession(null, null);
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
        throw new Error(detail || `${response.status} ${response.statusText}`);
      }
      return response.status === 204 ? null : response.json();
    },

    navigate() {
      const [name, id] = location.hash.replace(/^#\/?/, "").split("/");
      const singular = { bots: "bot", tournaments: "tournament", matches: "match" };
      this.route = id ? singular[name] : name || "leaderboard";
      this.params = id ? [id] : [];
      this.load();
    },

    async load() {
      clearTimeout(this.poll);
      this.error = "";
      this.data = null;
      const route = this.route;
      const id = this.params[0];
      try {
        let data;
        if (route === "leaderboard") data = await this.api("/leaderboard");
        else if (route === "bots") data = await this.api("/bots" + (this.statusFilter ? `?status=${this.statusFilter}` : ""));
        else if (route === "bot") data = await this.api(`/bots/${id}`);
        else if (route === "tournaments") data = await this.api("/tournaments");
        else if (route === "tournament") data = await this.api(`/tournaments/${id}`);
        else if (route === "match") data = await this.api(`/matches/${id}`);
        else if (route === "submit") data = await this.loadMyBots();
        else if (route === "play") data = await this.loadPlay();
        if (route === this.route) this.data = data;
      } catch (e) {
        this.error = e.message;
      }
    },

    async loadMyBots() {
      this.requireLogin();
      const bots = await this.api(`/bots?username=${encodeURIComponent(this.user)}`);
      if (bots.some((b) => b.status === "pending")) this.poll = setTimeout(() => this.refresh("submit"), 2000);
      return bots;
    },

    async refresh(route) {
      if (this.route !== route) return;
      try {
        this.data = await (route === "submit" ? this.loadMyBots() : this.loadPlay());
      } catch (e) {
        this.error = e.message;
      }
    },

    requireLogin() {
      if (!this.token) throw new Error("log in first");
    },

    async upload(input) {
      this.busy = true;
      this.error = "";
      try {
        const form = new FormData();
        form.append("file", input.files[0]);
        await this.api(`/accounts/${encodeURIComponent(this.user)}/bot`, { method: "POST", body: form });
        input.value = "";
        await this.refresh("submit");
      } catch (e) {
        this.error = e.message;
      } finally {
        this.busy = false;
      }
    },

    async loadPlay() {
      this.requireLogin();
      const [bots] = await Promise.all([
        this.api("/bots?status=active"),
        this.api("/game").then((g) => this.setGame(g), () => this.setGame(null)),
      ]);
      return bots;
    },

    setGame(game) {
      clearTimeout(this.poll);
      this.game = game;
      if (game?.observation) this.raiseTo = game.observation.min_raise_to;
      if (game && (game.status === "starting" || game.status === "thinking")) {
        this.poll = setTimeout(() => this.pollGame(), 1000);
      }
    },

    async pollGame() {
      if (this.route !== "play") return;
      try {
        this.setGame(await this.api("/game"));
      } catch (e) {
        this.error = e.message;
      }
    },

    async startGame() {
      this.busy = true;
      this.error = "";
      const [kind, value] = this.opponent.split(":");
      const body = kind === "bot" ? { bot_id: Number(value) } : { opponent: value };
      try {
        this.setGame(await this.api("/game", this.json("POST", body)));
      } catch (e) {
        this.error = e.message;
      } finally {
        this.busy = false;
      }
    },

    async act(type, amount = 0) {
      this.error = "";
      try {
        this.setGame(await this.api("/game/action", this.json("POST", { type, amount })));
      } catch (e) {
        this.error = e.message;
      }
    },

    async quitGame() {
      await this.api("/game", { method: "DELETE" }).catch((e) => (this.error = e.message));
      this.pollGame();
    },

    json(method, body) {
      return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    },

    handSummary(hand) {
      const delta = hand.deltas[0];
      const result = delta > 0 ? `you won ${delta}` : delta < 0 ? `you lost ${-delta}` : "split pot";
      if (!hand.showdown) return `${result} — ${hand.winners[0] === 0 ? "opponent" : "you"} folded`;
      return `${result} at showdown — ${hand.hand_classes[0]} vs ${hand.hand_classes[1]}`;
    },

    card(c) {
      return c === "??" ? "??" : c[0].replace("T", "10") + SUITS[c[1]];
    },

    suitClass(c) {
      return c[1] === "h" || c[1] === "d" ? "red" : "";
    },

    botLabel(id) {
      const d = this.data;
      if (id === d.bot_a) return `${d.bot_a_username}/${d.bot_a_name}`;
      if (id === d.bot_b) return `${d.bot_b_username}/${d.bot_b_name}`;
      return "-";
    },

    date(value) {
      return value ? new Date(value).toLocaleString() : "";
    },
  };
}
