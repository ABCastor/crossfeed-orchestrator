package main

import (
	"embed"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"sort"
	"strings"
	"time"
)

// The tray's title: the same icon and text the web panel's Settings previews (model.js barTitle),
// chosen by the `menubar` block of panel.json. The glance is read a second time here into the
// shapes below, which carry the numbers as the glance writes them (fractions included).

//go:embed icons/*.png
var iconFiles embed.FS

type barPace struct {
	Status       string   `json:"status"`
	Points       *float64 `json:"points"`
	Words        string   `json:"words"`
	DeltaPercent *float64 `json:"delta_percent"`
}

type barLimit struct {
	Title   string   `json:"title"`
	Kind    string   `json:"kind"`
	Minutes *float64 `json:"minutes"`
	Used    *float64 `json:"used_percent"`
	Active  *bool    `json:"active"`
	Scope   string   `json:"scope"`
	ResetAt string   `json:"reset_at"`
	Pace    *barPace `json:"pace"`
}

type barPool struct {
	Pool      string     `json:"pool"`
	Name      string     `json:"name"`
	Measured  bool       `json:"measured"`
	Used      *float64   `json:"used_percent"`
	Pace      *barPace   `json:"pace"`
	Limits    []barLimit `json:"limits"`
	UsageCost *struct {
		TodayUSD *float64 `json:"today_usd"`
	} `json:"usage_cost"`
	API *struct {
		Spend *struct {
			TodayUSD *float64 `json:"today_usd"`
		} `json:"spend"`
	} `json:"api"`
}

type barView struct {
	Pools []barPool `json:"pools"`
}

// barPrefs is panel.json's `menubar` block; an absent or unknown value is the default.
type barPrefs struct {
	Icon   string `json:"icon"`
	Show   string `json:"show"`
	Window string `json:"window"`
	Count  string `json:"count"`
	Text   string `json:"text"`
	Extra  string `json:"extra"`
	Reset  string `json:"reset"`
}

// panelPrefs is the part of panel.json the tray reads.
type panelPrefs struct {
	Menubar barPrefs `json:"menubar"`
	Refresh struct {
		OnOpen       *bool `json:"on_open"`
		EveryMinutes *int  `json:"every_minutes"`
	} `json:"refresh"`
	OpenOn  string `json:"open_on"`
	LastTab string `json:"last_tab"`
}

func oneOf(value string, allowed ...string) bool {
	for _, a := range allowed {
		if value == a {
			return true
		}
	}
	return false
}

// normalized fills the defaults panel_prefs.py fills, so a missing key behaves the same on both sides.
func (p barPrefs) normalized() barPrefs {
	if !oneOf(p.Icon, "crossfeed", "provider", "none") {
		p.Icon = "provider"
	}
	if !oneOf(p.Text, "full", "window", "number", "none") {
		p.Text = "window"
	}
	if !oneOf(p.Extra, "none", "reset", "pace", "cost") {
		p.Extra = "none"
	}
	if !oneOf(p.Reset, "countdown", "clock") {
		p.Reset = "countdown"
	}
	if !oneOf(p.Window, "tightest", "5h", "Week", "Month") {
		p.Window = "tightest"
	}
	if !oneOf(p.Count, "used", "left") {
		p.Count = "used"
	}
	if p.Show == "" {
		p.Show = "pressed"
	}
	if p.Icon == "none" && p.Text == "none" {
		p.Icon = "crossfeed" // an item with no icon and no text would be invisible
	}
	return p
}

// everyMinutes is the background reading interval in minutes: 0 is off, up to a day; anything else is the default 5.
func (p panelPrefs) everyMinutes() int {
	if p.Refresh.EveryMinutes != nil && *p.Refresh.EveryMinutes >= 0 && *p.Refresh.EveryMinutes <= 1440 {
		return *p.Refresh.EveryMinutes
	}
	return 5
}

var tabID = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,39}$`)

// barFragment is where "Open panel" lands: a named tab, else nothing, and the page opens on the last tab used.
func (p panelPrefs) barFragment() string {
	if p.OpenOn != "last" && tabID.MatchString(p.OpenOn) {
		return "tab=" + p.OpenOn
	}
	return ""
}

type barTitleResult struct {
	Text    string
	Icon    string // crossfeed, provider or none
	Logo    string // icon file name without .png, when Icon is provider
	Tooltip string
}

// group is the tab a pool belongs to: the ids the panel preferences use.
func group(pool string) string {
	switch {
	case pool == "antigravity-gemini" || pool == "antigravity-3p" || pool == "antigravity-argon":
		return "antigravity"
	case strings.HasPrefix(pool, "github-copilot"):
		return "copilot"
	case strings.HasPrefix(pool, "opencode-go"):
		return "opencode"
	}
	return pool
}

var titles = map[string]string{"claude": "Claude", "codex": "Codex", "copilot": "Copilot", "antigravity": "AGY", "opencode": "OpenCode",
	"openrouter-free": "OpenRouter", "gemini-metered": "Gemini API", "chatgpt-work": "Crossfeed Chat"}
var agyNames = map[string]string{"antigravity-gemini": "Gemini", "antigravity-3p": "Claude/GPT", "antigravity-argon": "Argon"}
var logos = map[string]string{"claude": "claude", "codex": "openai", "copilot": "githubcopilot", "antigravity": "antigravity",
	"opencode": "opencode", "openrouter-free": "openrouter", "gemini-metered": "gemini", "chatgpt-work": "openai"}

func barWindow(l *barLimit) string {
	if l == nil {
		return "Usage"
	}
	if l.Minutes != nil {
		m := *l.Minutes
		switch {
		case m == 300:
			return "5h"
		case m == 1440:
			return "Day"
		case m == 10080:
			return "Week"
		case m >= 40000 && m <= 45000:
			return "Month"
		case m < 1440:
			return fmt.Sprintf("%.0fh", math.Round(m/60))
		}
	}
	t := strings.ToLower(l.Title)
	switch {
	case strings.Contains(t, "5-hour") || strings.Contains(t, "5h"):
		return "5h"
	case strings.Contains(t, "week"):
		return "Week"
	case strings.Contains(t, "month"):
		return "Month"
	case strings.Contains(t, "day"):
		return "Day"
	case strings.Contains(t, "session"):
		return "Session"
	}
	word := "Usage"
	if f := strings.Fields(l.Title); len(f) > 0 {
		word = f[0]
	}
	if r := []rune(word); len(r) > 8 {
		word = string(r[:8])
	}
	return word
}

var percentWords = regexp.MustCompile(`(\d+)%`)

func (p *barPace) points() float64 {
	if p == nil {
		return 0
	}
	if p.Points != nil {
		return *p.Points
	}
	if m := percentWords.FindStringSubmatch(p.Words); m != nil {
		var n float64
		fmt.Sscan(m[1], &n)
		return n
	}
	if p.DeltaPercent != nil {
		return math.Abs(math.Round(*p.DeltaPercent))
	}
	return 0
}

func (p *barPace) deficit() float64 {
	if p != nil && p.Status == "deficit" {
		return p.points()
	}
	return 0
}

func countdown(iso string, now time.Time) string {
	t, err := time.Parse(time.RFC3339, iso)
	if err != nil {
		return ""
	}
	s := int(t.Sub(now).Seconds())
	if s <= 0 {
		return "now"
	}
	d, h, m := s/86400, (s%86400)/3600, (s%3600)/60
	switch {
	case d > 0:
		return fmt.Sprintf("%dd %dh", d, h)
	case h > 0:
		return fmt.Sprintf("%dh %dm", h, m)
	}
	if m < 1 {
		m = 1
	}
	return fmt.Sprintf("%dm", m)
}

type pressed struct {
	pool    *barPool
	limit   *barLimit
	used    float64
	deficit float64
}

func windowsOf(p *barPool) []*barLimit {
	var out []*barLimit
	for i := range p.Limits {
		l := &p.Limits[i]
		if l.Kind == "window" && l.Used != nil && (l.Active == nil || *l.Active) {
			out = append(out, l)
		}
	}
	return out
}

// barTitle is the tray's title for a glance and the operator's choices. It is a plain function of its
// inputs so the same fixture can be checked here and in the panel's own tests.
func barTitle(view barView, prefs barPrefs, now time.Time) barTitleResult {
	prefs = prefs.normalized()
	var ranked []pressed
	if prefs.Window != "tightest" {
		for i := range view.Pools {
			p := &view.Pools[i]
			if !p.Measured || p.Used == nil {
				continue
			}
			var hit *barLimit
			for _, l := range windowsOf(p) {
				if barWindow(l) == prefs.Window && l.Scope == "" {
					hit = l
					break
				}
			}
			if hit == nil {
				for _, l := range windowsOf(p) {
					if barWindow(l) == prefs.Window {
						hit = l
						break
					}
				}
			}
			if hit != nil {
				ranked = append(ranked, pressed{p, hit, *hit.Used, hit.Pace.deficit()})
			}
		}
	}
	if len(ranked) == 0 {
		for i := range view.Pools {
			p := &view.Pools[i]
			if !p.Measured || p.Used == nil {
				continue
			}
			ws := windowsOf(p)
			var limit *barLimit
			for _, l := range ws {
				if math.Abs(*l.Used-*p.Used) < 0.5 && l.Scope == "" {
					limit = l
					break
				}
			}
			if limit == nil {
				for _, l := range ws {
					if math.Abs(*l.Used-*p.Used) < 0.5 {
						limit = l
						break
					}
				}
			}
			if limit == nil && len(ws) > 0 {
				limit = ws[0]
				for _, l := range ws {
					if *l.Used > *limit.Used {
						limit = l
					}
				}
			}
			ranked = append(ranked, pressed{p, limit, *p.Used, p.Pace.deficit()})
		}
	}
	sort.SliceStable(ranked, func(a, b int) bool {
		if ranked[a].deficit != ranked[b].deficit {
			return ranked[a].deficit > ranked[b].deficit
		}
		return ranked[a].used > ranked[b].used
	})
	if len(ranked) == 0 {
		icon := "crossfeed"
		if prefs.Icon == "none" {
			icon = "none"
		}
		return barTitleResult{Icon: icon, Tooltip: "Crossfeed · no quota read"}
	}
	focal := ranked[0]
	if prefs.Show != "pressed" {
		for _, r := range ranked {
			if group(r.pool.Pool) == prefs.Show {
				focal = r
				break
			}
		}
	}
	tab := group(focal.pool.Pool)
	name := agyNames[focal.pool.Pool]
	if name == "" {
		name = titles[tab]
	}
	if name == "" {
		name = focal.pool.Name
		if name == "" {
			name = focal.pool.Pool
		}
		if r := []rune(name); len(r) > 14 {
			name = string(r[:14])
		}
	}
	window := barWindow(focal.limit)
	value := focal.used
	amountSuffix := "%"
	if prefs.Count == "left" {
		value = 100 - focal.used
		amountSuffix = "% left"
	}
	amount := fmt.Sprintf("%d%s", int(math.Max(0, math.Min(100, math.Round(value)))), amountSuffix)
	var text string
	switch prefs.Text {
	case "full":
		text = name + " " + window + " " + amount
	case "window":
		text = window + " " + amount
	case "number":
		text = amount
	}
	var pace *barPace
	if focal.limit != nil && focal.limit.Pace != nil {
		pace = focal.limit.Pace
	} else {
		pace = focal.pool.Pace
	}
	var extra string
	switch prefs.Extra {
	case "reset":
		if focal.limit != nil && focal.limit.ResetAt != "" {
			if prefs.Reset == "clock" {
				if t, err := time.Parse(time.RFC3339, focal.limit.ResetAt); err == nil {
					extra = t.In(now.Location()).Format("Mon 15:04")
				}
			} else {
				extra = countdown(focal.limit.ResetAt, now)
			}
		}
	case "pace":
		switch {
		case pace != nil && pace.Status == "deficit":
			extra = fmt.Sprintf("%.0f%% over", pace.points())
		case pace != nil && pace.Status == "surplus":
			extra = fmt.Sprintf("%.0f%% under", pace.points())
		case pace != nil && pace.Status == "on_pace":
			extra = "on pace"
		}
	case "cost":
		var sum float64
		for _, p := range view.Pools {
			if p.UsageCost != nil && p.UsageCost.TodayUSD != nil {
				sum += *p.UsageCost.TodayUSD
			}
			if p.API != nil && p.API.Spend != nil && p.API.Spend.TodayUSD != nil {
				sum += *p.API.Spend.TodayUSD
			}
		}
		if sum > 0 {
			extra = fmt.Sprintf("US$%.2f today", sum)
		}
	}
	if text != "" && extra != "" {
		text += " · " + extra
	}
	logo := logos[tab]
	icon := prefs.Icon
	if icon == "provider" && logo == "" {
		icon = "crossfeed"
	}
	tip := fmt.Sprintf("%s %s: %.0f%% used", name, window, focal.used)
	if focal.used >= 100 {
		tip = name + " used up"
	}
	return barTitleResult{Text: text, Icon: icon, Logo: logo, Tooltip: tip}
}

// logoPNG is the embedded rasterised logo for a name from `logos`, or nil.
func logoPNG(name string) []byte {
	if name == "" {
		return nil
	}
	data, err := iconFiles.ReadFile("icons/" + name + ".png")
	if err != nil {
		return nil
	}
	return data
}

func decodeBar(raw []byte) barView {
	var view barView
	_ = json.Unmarshal(raw, &view)
	return view
}
