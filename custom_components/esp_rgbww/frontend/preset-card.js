class LightinatorPresetCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._busy = false;
    this._error = "";
  }

  setConfig(config) {
    if (!config.entity || !config.entity.startsWith("light.")) {
      throw new Error("Specify a Lightinator light entity.");
    }
    this._config = { ...config };
    this.render();
  }

  set hass(hass) {
    this._hass = hass;
    this.render();
  }

  getCardSize() { return 2; }

  render() {
    if (!this._config || !this._hass) return;
    const state = this._hass.states[this._config.entity];
    this.shadowRoot.replaceChildren();
    const style = document.createElement("style");
    style.textContent = `
      ha-card { padding:16px; }
      h3 { margin:0 0 12px; font-size:var(--ha-font-size-l,18px); }
      .swatches { display:flex; flex-wrap:wrap; gap:12px; }
      button { display:flex; align-items:center; gap:8px; padding:8px 12px;
        color:var(--primary-text-color); background:var(--secondary-background-color);
        border:1px solid var(--divider-color); border-radius:12px; cursor:pointer; }
      button:focus-visible { outline:2px solid var(--primary-color); }
      button:disabled { opacity:.5; cursor:default; }
      .dot { width:28px; height:28px; border-radius:50%; border:1px solid var(--divider-color); }
      .error { color:var(--error-color); margin-top:12px; }
    `;
    const card = document.createElement("ha-card");
    const title = document.createElement("h3");
    title.textContent = this._config.title || state?.attributes.friendly_name || "Lightinator presets";
    card.append(title);
    const swatches = state?.attributes.lightinator_presets;
    const available = state && !["unavailable", "unknown"].includes(state.state);
    if (!Array.isArray(swatches) || swatches.length === 0) {
      const empty = document.createElement("div");
      empty.textContent = available ? "No favourite presets. Star colours on this light's web UI Presets page." : "Light unavailable.";
      card.append(empty);
    } else {
      const container = document.createElement("div");
      container.className = "swatches";
      for (const preset of swatches) {
        const button = document.createElement("button");
        button.type = "button";
        button.disabled = this._busy || !available;
        button.title = String(preset.name);
        const dot = document.createElement("span");
        dot.className = "dot";
        const rgb = preset.rgb;
        if (Array.isArray(rgb) && rgb.length === 3 && rgb.every((value) => Number.isInteger(value) && value >= 0 && value <= 255)) {
          dot.style.backgroundColor = `rgb(${rgb.join(",")})`;
        }
        const label = document.createElement("span");
        label.textContent = String(preset.name);
        button.append(dot, label);
        button.addEventListener("click", () => this.apply(preset.id));
        container.append(button);
      }
      card.append(container);
    }
    if (this._error) {
      const error = document.createElement("div");
      error.className = "error";
      error.setAttribute("role", "alert");
      error.textContent = this._error;
      card.append(error);
    }
    this.shadowRoot.append(style, card);
  }

  async apply(presetId) {
    if (this._busy) return;
    this._busy = true;
    this._error = "";
    this.render();
    try {
      await this._hass.callWS({
        type: "esp_rgbww/preset",
        entity_id: this._config.entity,
        preset_id: presetId,
        action: "apply",
      });
    } catch (error) {
      this._error = error.message || String(error);
    } finally {
      this._busy = false;
      this.render();
    }
  }
}

if (!customElements.get("lightinator-preset-card")) {
  customElements.define("lightinator-preset-card", LightinatorPresetCard);
}
window.customCards = window.customCards || [];
if (!window.customCards.some((card) => card.type === "lightinator-preset-card")) {
  window.customCards.push({
    type: "lightinator-preset-card",
    name: "Lightinator presets",
    description: "Controller-stored favourite swatches for one Lightinator light.",
  });
}
