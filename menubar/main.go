// Crossfeed renders the local fleetctl view. No provider credentials enter the app.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"log"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
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
	Title  string `json:"title"`
	Pools  []pool `json:"pools"`
	Spend  *spend `json:"subscription_spend"`
	// Bar is the same document read again for the menu bar title, which needs the numbers as fractions.
	Bar barView `json:"-"`
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

// providerRows is the provider part of rows(): one line per pool, with its limits under it.
func providerRows(data snapshot) []row {
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
	return out
}

func rows(data snapshot) []row {
	out := providerRows(data)
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

// menuRows is what the tray's menu shows: one line per provider with its tightest window.
// The per-limit lines stay in the panel, where there is room to read them.
func menuRows(data snapshot) []row {
	out := providerRows(data)
	for i := range out {
		out[i].children = nil
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
	data.Bar = decodeBar(out)
	return data, nil
}

// args is the fleetctl command line for one subcommand, with the overlay and state directory this tray was given.
func (c config) args(sub ...string) []string {
	args := []string{c.fleetctl}
	if c.overlay != "" {
		args = append(args, "--overlay", c.overlay)
	}
	if c.state != "" {
		args = append(args, "--state-dir", c.state)
	}
	return append(args, sub...)
}

// prefs reads panel.json through fleetctl, which fills every default, so the tray and the panel agree.
// An unreadable answer is the defaults: the tray still draws.
func (c config) prefs(ctx context.Context) panelPrefs {
	ctx, cancel := context.WithTimeout(ctx, 8*time.Second)
	defer cancel()
	var prefs panelPrefs
	out, err := exec.CommandContext(ctx, c.python, c.args("panel", "get")...).Output()
	if err != nil || len(out) > 1<<20 || json.Unmarshal(out, &prefs) != nil {
		return panelPrefs{}
	}
	return prefs
}

// poll asks fleetctl to read the sources whose reading is due, the same way the scheduled poll does: its output is
// discarded and it stops after the reading. The next load picks the new numbers up from the cache.
func (c config) poll(ctx context.Context) error {
	ctx, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	return exec.CommandContext(ctx, c.python, c.args("glance")...).Run()
}

// The console's own startup line, and nothing else: a loopback sign-in link this tray can trust.
const signInPrefix = "Sign-in link for this launch: "

// signedLink accepts only http://127.0.0.1:PORT/login?key=... — the line fleetctl's console prints.
func signedLink(line string) (string, bool) {
	line = strings.TrimSpace(line)
	if !strings.HasPrefix(line, signInPrefix) {
		return "", false
	}
	u, err := url.Parse(strings.TrimPrefix(line, signInPrefix))
	if err != nil || u.Scheme != "http" || u.Hostname() != "127.0.0.1" || u.Port() == "" || u.Path != "/login" ||
		u.User != nil || u.Fragment != "" {
		return "", false
	}
	query := u.Query()
	if len(query) != 1 || query.Get("key") == "" {
		return "", false
	}
	return u.String(), true
}

// pageURL aims a signed-in link at one local page. The console's 303 has no fragment, so the page
// named in `next` is where the browser lands; a fixed path, never something taken from a reading.
func pageURL(link, page string) string {
	u, err := url.Parse(link)
	if err != nil || page == "" {
		return link
	}
	query := u.Query()
	query.Set("next", page)
	u.RawQuery = query.Encode()
	return u.String()
}

// pageURLAt is pageURL plus a fragment (the panel's `#tab=<id>`); the fragment stays on the browser side
// of the console's sign-in redirect, so the page opens on that tab.
func pageURLAt(link, page, fragment string) string {
	target := pageURL(link, page)
	if fragment == "" {
		return target
	}
	return target + "#" + fragment
}

// browserCommand prefers an app-style window, which is the closest a Linux desktop gets to a menu
// bar panel: no tabs, no address bar. Without one of those browsers it is xdg-open's problem.
func browserCommand(target string) *exec.Cmd {
	for _, name := range []string{"chromium", "chromium-browser", "google-chrome", "google-chrome-stable"} {
		if path, err := exec.LookPath(name); err == nil {
			return exec.Command(path, "--app="+target, "--class=Crossfeed")
		}
	}
	return exec.Command("xdg-open", target)
}

func openTarget(ctx context.Context, target string) error {
	if runtime.GOOS == "darwin" {
		return exec.CommandContext(ctx, "/usr/bin/open", target).Run()
	}
	cmd := browserCommand(target)
	// The browser outlives this call on purpose; its output is not ours to collect.
	cmd.Stdout, cmd.Stderr = io.Discard, io.Discard
	return cmd.Start()
}

// consoleSession is the console the tray itself starts, so "Open panel" is signed in without the
// operator copying a link. One console per tray; it stops when the tray does.
type consoleSession struct {
	cmd  *exec.Cmd
	link chan string
	mu   sync.Mutex
	url  string
}

func startConsole(c config) (*consoleSession, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	args := []string{c.fleetctl}
	if c.overlay != "" {
		args = append(args, "--overlay", c.overlay)
	}
	if c.state != "" {
		args = append(args, "--state-dir", c.state)
	}
	args = append(args, "console", "--no-open")
	cmd := exec.CommandContext(ctx, c.python, args...)
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	cmd.Stderr = io.Discard
	if err := cmd.Start(); err != nil {
		return nil, err
	}
	// The command context ends with this function; the process keeps its own lifetime.
	cmd.Cancel = nil
	cmd.WaitDelay = 0
	session := &consoleSession{cmd: cmd, link: make(chan string, 1)}
	go func() {
		scanner := bufio.NewScanner(stdout)
		for scanner.Scan() {
			if link, ok := signedLink(scanner.Text()); ok {
				select {
				case session.link <- link:
				default:
				}
			}
		}
	}()
	return session, nil
}

// signed answers with the console's sign-in link, waiting a moment for a cold start.
func (s *consoleSession) signed() (string, error) {
	s.mu.Lock()
	cached := s.url
	s.mu.Unlock()
	if cached != "" {
		return cached, nil
	}
	select {
	case link := <-s.link:
		s.mu.Lock()
		s.url = link
		s.mu.Unlock()
		return link, nil
	case <-time.After(15 * time.Second):
		return "", errors.New("the console printed no sign-in link")
	}
}

// open shows one page signed in, the way the Mac app's deep links do.
func (s *consoleSession) open(ctx context.Context, page, fragment string) error {
	link, err := s.signed()
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	return openTarget(ctx, pageURLAt(link, page, fragment))
}

func (s *consoleSession) stop() {
	if s == nil || s.cmd == nil || s.cmd.Process == nil {
		return
	}
	_ = s.cmd.Process.Kill()
	_, _ = s.cmd.Process.Wait()
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
	item *systray.MenuItem
}

type menu struct {
	rows                []menuRow
	panel, usage, setup *systray.MenuItem
	refresh, quit       *systray.MenuItem
	session             *consoleSession
}

// paint shows one line per provider and the four things the tray can do. The menu is rebuilt only
// when its shape changes: a title update is what the minute's refresh needs.
func (m *menu) paint(lines []row) {
	shape := len(lines) == len(m.rows) && m.panel != nil
	if !shape {
		systray.ResetMenu()
		m.rows = nil
		for _, line := range lines {
			item := systray.AddMenuItem(line.title, "")
			item.Disable()
			m.rows = append(m.rows, menuRow{item: item})
		}
		systray.AddSeparator()
		m.panel = systray.AddMenuItem("Open panel", "The tiles, signed in")
		m.usage = systray.AddMenuItem("Usage and cost", "What each provider cost and how it was used")
		m.setup = systray.AddMenuItem("Settings", "Plans, shares and spend policy")
		m.refresh = systray.AddMenuItem("Refresh now", "Read every provider again")
		systray.AddSeparator()
		m.quit = systray.AddMenuItem("Quit", "")
		return
	}
	for i, line := range lines {
		m.rows[i].item.SetTitle(line.title)
	}
}

func start(ctx context.Context, c config) {
	systray.SetTemplateIcon(icon(color.Black), icon(color.NRGBA{R: 94, G: 135, B: 255, A: 255}))
	systray.SetTitle("Crossfeed")
	systray.SetTooltip("Crossfeed · quotas and subscriptions")
	session, err := startConsole(c)
	if err != nil {
		log.Printf("Cannot start the Crossfeed console: %v", err)
	}
	var m menu
	m.session = session
	m.paint([]row{{title: "Reading quotas…"}})
	// D-Bus GetLayout waits for this ready callback to return. Poll separately.
	go m.run(ctx, c)
}

// setTitle draws the title the operator chose in Settings > Menu bar: the icon (the Crossfeed mark, the
// provider's logo, or nothing) and the text. AppIndicator hosts differ: some draw only the icon.
func setTitle(result barTitleResult) {
	switch result.Icon {
	case "none":
		systray.SetIcon(blankIcon())
	case "provider":
		if data := logoPNG(result.Logo); data != nil {
			systray.SetIcon(data)
		} else {
			systray.SetTemplateIcon(icon(color.Black), icon(color.NRGBA{R: 94, G: 135, B: 255, A: 255}))
		}
	default:
		systray.SetTemplateIcon(icon(color.Black), icon(color.NRGBA{R: 94, G: 135, B: 255, A: 255}))
	}
	systray.SetTitle(short(result.Text, 60))
	systray.SetTooltip(result.Tooltip)
}

// blankIcon is a transparent icon: AppIndicator wants one even when the operator turned the icon off.
func blankIcon() []byte {
	var out bytes.Buffer
	_ = png.Encode(&out, image.NewNRGBA(image.Rect(0, 0, 22, 22)))
	return out.Bytes()
}

func (menu *menu) run(ctx context.Context, c config) {
	type update struct {
		data  snapshot
		prefs panelPrefs
		err   error
	}
	updates := make(chan update, 1)
	loading, polling := false, false
	polled := make(chan struct{}, 1)
	refresh := func() {
		if !loading {
			loading = true
			go func() {
				prefs := c.prefs(ctx)
				data, err := c.load(ctx)
				updates <- update{data, prefs, err}
			}()
		}
	}
	// poll reads the due sources, then the cache again; one at a time, and never on the menu's own thread.
	poll := func() {
		if polling {
			return
		}
		polling = true
		go func() {
			_ = c.poll(ctx)
			polled <- struct{}{}
		}()
	}
	var prefs panelPrefs
	open := func(page, fragment string) {
		go func() {
			if menu.session == nil || menu.session.open(ctx, page, fragment) != nil {
				log.Printf("Cannot open the Crossfeed %s page", page)
			}
		}()
	}
	refresh()
	poll() // the first reading on start-up: a fresh install has nothing cached yet
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	sinceRead := 0
	for {
		select {
		case <-ctx.Done():
			menu.session.stop()
			systray.Quit()
			return
		case <-menu.quit.ClickedCh:
			menu.session.stop()
			systray.Quit()
			return
		case <-menu.panel.ClickedCh:
			open("/panel", prefs.barFragment())
		case <-menu.usage.ClickedCh:
			open("/usage", "")
		case <-menu.setup.ClickedCh:
			open("/settings", "")
		case <-menu.refresh.ClickedCh:
			poll()
		case <-polled:
			polling = false
			refresh()
		case <-ticker.C:
			// The cache is re-read every minute (cheap, and it carries a change made in the panel's
			// Settings); the sources are read again every N minutes, the operator's Refresh interval.
			sinceRead++
			if every := prefs.everyMinutes(); every > 0 && sinceRead >= every {
				sinceRead = 0
				poll()
			}
			refresh()
		case u := <-updates:
			loading = false
			prefs = u.prefs
			if u.err != nil {
				systray.SetTitle("Crossfeed")
				menu.paint([]row{{title: "Quotas unavailable"}})
				continue
			}
			setTitle(barTitle(u.data.Bar, prefs.Menubar, time.Now()))
			menu.paint(menuRows(u.data))
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
