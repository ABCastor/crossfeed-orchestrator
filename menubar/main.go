// Crossfeed renders the local fleetctl view. No provider credentials enter the app.
package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"log"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"syscall"
	"time"

	"fyne.io/systray"
)

type pace struct {
	Words string `json:"words"`
}

type limit struct {
	Title string `json:"title"`
	Used  *int   `json:"used_percent"`
	Reset string `json:"resets"`
	Pace  pace   `json:"pace"`
}

type pool struct {
	Name       string          `json:"name"`
	Measured   bool            `json:"measured"`
	Used       *int            `json:"used_percent"`
	Reset      string          `json:"resets"`
	Pace       pace            `json:"pace"`
	Limits     []limit         `json:"limits"`
	GaugeError json.RawMessage `json:"gauge_error"`
	Stale      *int            `json:"stale_age_s"`
}

type monthly struct {
	Currency string  `json:"currency"`
	Total    string  `json:"total"`
	Operator *string `json:"operator"`
}

type spend struct {
	Monthly     []monthly `json:"monthly"`
	Unpriced    int       `json:"unpriced"`
	Unavailable bool      `json:"unavailable"`
}

type snapshot struct {
	Schema int    `json:"schema"`
	Pools  []pool `json:"pools"`
	Spend  *spend `json:"subscription_spend"`
}

type row struct {
	title    string
	children []string
}

// Native labels are never executable markup. Collapse whitespace and cap long provider strings.
func short(s string, n int) string {
	s = strings.Join(strings.Fields(s), " ")
	if r := []rune(s); len(r) > n {
		return string(r[:n-1]) + "…"
	}
	return s
}

func quota(used *int, reset string, p pace, compact bool) string {
	if used == nil || *used < 0 || *used > 100 {
		return "Not read"
	}
	label := fmt.Sprintf("%d%% left", 100-*used)
	if reset != "" {
		label += " · " + short(reset, 24)
	} else {
		label += " · reset unknown"
	}
	words := p.Words
	if compact {
		words, _, _ = strings.Cut(words, ", ")
	}
	if words != "" {
		label += " · " + short(words, 70)
	}
	return label
}

func rows(data snapshot) []row {
	var out []row
	for _, p := range data.Pools {
		label := quota(p.Used, p.Reset, p.Pace, true)
		failed := len(p.GaugeError) != 0 && string(p.GaugeError) != "null"
		if p.Used == nil && !p.Measured && !failed {
			label = "No quota window"
		} else if (failed && p.Used != nil) || p.Stale != nil {
			label += " · stale"
		}
		r := row{title: short(p.Name, 24) + "  " + label}
		for _, l := range p.Limits {
			r.children = append(r.children, short(l.Title, 25)+"  "+quota(l.Used, l.Reset, l.Pace, false))
		}
		out = append(out, r)
	}
	if len(out) == 0 {
		out = append(out, row{title: "No pools configured"})
	}
	s := data.Spend
	switch {
	case s == nil || s.Unavailable:
		out = append(out, row{title: "Spend unavailable"})
	case len(s.Monthly) == 0:
		if s.Unpriced > 0 {
			out = append(out, row{title: "Month · Add prices in the console"})
		} else {
			out = append(out, row{title: "No subscriptions configured"})
		}
	default:
		for _, m := range s.Monthly {
			title := "Month · " + short(m.Currency, 3) + " " + short(m.Total, 15)
			if m.Operator != nil {
				title += " · yours " + short(*m.Operator, 15)
			} else {
				title += " · share not set"
			}
			if s.Unpriced > 0 {
				title += " · partial"
			}
			out = append(out, row{title: title})
		}
	}
	return out
}

type config struct {
	python, fleetctl, overlay, state string
}

func (c config) load(ctx context.Context) (snapshot, error) {
	ctx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	args := []string{c.fleetctl}
	if c.overlay != "" {
		args = append(args, "--overlay", c.overlay)
	}
	if c.state != "" {
		args = append(args, "--state-dir", c.state)
	}
	args = append(args, "glance", "--json", "--no-refresh")
	cmd := exec.CommandContext(ctx, c.python, args...)
	out, err := cmd.Output()
	if err != nil || len(out) > 2<<20 {
		return snapshot{}, errors.New("cannot read fleetctl")
	}
	var data snapshot
	if json.Unmarshal(out, &data) != nil || data.Schema != 1 || data.Pools == nil || data.Spend == nil {
		return snapshot{}, errors.New("unsupported fleetctl glance format")
	}
	return data, nil
}

func openConsole(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	const consoleURL = "http://127.0.0.1:8768/"
	if runtime.GOOS == "darwin" {
		if home, err := os.UserHomeDir(); err == nil {
			launcher := filepath.Join(home, "Applications", "Crossfeed Orchestrator.app")
			if info, err := os.Stat(launcher); err == nil && info.IsDir() {
				return exec.CommandContext(ctx, "/usr/bin/open", launcher).Run()
			}
		}
		return exec.CommandContext(ctx, "/usr/bin/open", consoleURL).Run()
	}
	return exec.CommandContext(ctx, "xdg-open", consoleURL).Run()
}

// Draw a tiny three-level gauge. A template image adapts to the macOS menu-bar color.
func icon(c color.Color) []byte {
	img := image.NewNRGBA(image.Rect(0, 0, 18, 18))
	for i, h := range []int{6, 10, 14} {
		for x := 2 + i*5; x < 5+i*5; x++ {
			for y := 16 - h; y < 16; y++ {
				img.Set(x, y, c)
			}
		}
	}
	var out bytes.Buffer
	_ = png.Encode(&out, img)
	return out.Bytes()
}

type menuRow struct {
	item     *systray.MenuItem
	children []*systray.MenuItem
}

type menu struct {
	rows               []menuRow
	spendRows          int
	open, reload, quit *systray.MenuItem
}

func (m *menu) paint(lines []row, spendRows int) {
	shape := len(lines) == len(m.rows) && m.spendRows == spendRows && m.open != nil
	if shape {
		for i := range lines {
			shape = shape && len(m.rows[i].children) == len(lines[i].children)
		}
	}
	if !shape {
		systray.ResetMenu()
		m.rows, m.spendRows = nil, spendRows
		for i, line := range lines {
			if i > 0 && i == len(lines)-spendRows {
				systray.AddSeparator()
			}
			item := systray.AddMenuItem(line.title, "")
			entry := menuRow{item: item}
			if len(line.children) == 0 {
				item.Disable()
			}
			for _, text := range line.children {
				child := item.AddSubMenuItem(text, "")
				child.Disable()
				entry.children = append(entry.children, child)
			}
			m.rows = append(m.rows, entry)
		}
		systray.AddSeparator()
		m.open = systray.AddMenuItem("Open console", "")
		m.reload = systray.AddMenuItem("Reload view", "Read the saved readings again")
		m.quit = systray.AddMenuItem("Quit Crossfeed", "")
		return
	}
	for i, line := range lines {
		m.rows[i].item.SetTitle(line.title)
		for j, title := range line.children {
			m.rows[i].children[j].SetTitle(title)
		}
	}
}

func start(ctx context.Context, c config) {
	systray.SetTemplateIcon(icon(color.Black), icon(color.NRGBA{R: 94, G: 135, B: 255, A: 255}))
	systray.SetTitle("Crossfeed")
	systray.SetTooltip("Crossfeed · quotas and subscriptions")
	var m menu
	m.paint([]row{{title: "Reading quotas…"}}, 0)
	// D-Bus GetLayout waits for this ready callback to return. Poll separately.
	go m.run(ctx, c)
}

func (menu *menu) run(ctx context.Context, c config) {
	type update struct {
		data snapshot
		err  error
	}
	updates := make(chan update, 1)
	loading := false
	refresh := func() {
		if !loading {
			loading = true
			go func() {
				data, err := c.load(ctx)
				updates <- update{data, err}
			}()
		}
	}
	refresh()
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			systray.Quit()
			return
		case <-menu.quit.ClickedCh:
			systray.Quit()
			return
		case <-menu.open.ClickedCh:
			go func() {
				if openConsole(ctx) != nil {
					log.Print("Cannot open the Crossfeed console")
				}
			}()
		case <-menu.reload.ClickedCh:
			refresh()
		case <-ticker.C:
			refresh()
		case u := <-updates:
			loading = false
			if u.err != nil {
				menu.paint([]row{{title: "Quotas unavailable"}}, 0)
				continue
			}
			menu.paint(rows(u.data), max(1, len(u.data.Spend.Monthly)))
		}
	}
}

func main() {
	var c config
	flag.StringVar(&c.fleetctl, "fleetctl", "", "absolute path to the installed fleetctl.py")
	flag.StringVar(&c.python, "python", "python3", "Python interpreter")
	flag.StringVar(&c.overlay, "overlay", "", "use this access overlay instead of the default")
	flag.StringVar(&c.state, "state-dir", "", "use this state directory instead of the default")
	flag.Parse()
	if !filepath.IsAbs(c.fleetctl) {
		log.Fatal("Set -fleetctl to the absolute path to fleetctl.py")
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	systray.Run(func() { start(ctx, c) }, cancel)
}
