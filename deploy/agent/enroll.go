package main

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"

	"pystino-agent/internal/policy"
)

const enrollUsage = `pystino-agent enroll — sign in and write the opencode setup.

Usage:
  pystino-agent enroll [--issuer URL] [--gateway URL] [--client-id ID]
                [--device | --loopback] [--group NAME] [--output PATH]
                [--creds PATH] [--shim-port PORT] [--no-discover]
                [--allow-opencode-provider] [--yes]

  --issuer     OIDC issuer where discovery lives (prompted when missing).
  --gateway    Gateway origin or /v1 base, e.g. https://llm.example.org
               (a bare origin gets /v1 appended; prompted when missing).
  --cerea      Cerea origin 'run' dials by default, e.g. https://cerea.example.org
               (optional: 'run --cerea' overrides it; unset means 'run' has
               no default and --cerea becomes required on that command).
  --client-id  OAuth client id (default opencode-enrollment: the id baked
               into both bundled IdPs, so it must match on either).
  --device     Force the device flow (headless boxes).
  --loopback   Force the loopback browser flow (laptops).
  --group      Preselect the billing group (else prompted when several).
  --output     Where to write opencode.json (default ./opencode.json).
  --creds      Where to store the refresh credential (default
               <config-dir>/opencode/pystino-credentials.json), mode 0600.
  --shim-port  Preferred local port for the serve shim (default 41871;
               bumped upward while occupied, then recorded).
  --no-discover  Skip GET /v1/models; write a placeholder models map.
  --allow-opencode-provider  Leave opencode's built-in providers enabled.
               By default the written config names pystino in
               enabled_providers, so the gateway's models are the only
               ones opencode offers; this flag omits that allowlist for
               operators who want the built-ins too.
  --allow-auto-accept  Let 'run' permit session.setAutoAccept at all
               (default denied: the machine's own veto, PROTOCOL.md §4 —
               Cerea can never turn this on over the link if this flag was
               never passed at enroll time).
  --workspace-root PATH  Confine workspace.create to this path or below
               (repeatable; default unrestricted). Every occurrence is
               recorded; 'run' refuses a workspace outside all of them.
  --allow-free-models  Let 'run' list and accept models from providers
               other than the gateway's own (default: only pystino/*
               models, so spend always lands in the account this machine
               enrolled under).
  --yes        Overwrite existing files without asking.
`

// defaultShimPort is the loopback port serve listens on. Unprivileged and
// outside the well-known ephemeral ranges a browser flow might land on, so
// enroll's own callback listener and the shim never collide.
const defaultShimPort = 41871

type enrollOptions struct {
	issuer                 string
	gateway                string
	cerea                  string
	clientID               string
	device                 bool
	loopback               bool
	group                  string
	output                 string
	creds                  string
	shimPort               int
	discover               bool
	allowOpencodeProviders bool
	allowAutoAccept        bool
	workspaceRoots         []string
	allowFreeModels        bool
	yes                    bool
}

// stringListFlag implements flag.Value for a flag that may be repeated
// (--workspace-root), which the stdlib flag package has no direct support
// for: each occurrence appends rather than replacing.
type stringListFlag struct{ values *[]string }

func (f stringListFlag) String() string {
	if f.values == nil {
		return ""
	}
	return strings.Join(*f.values, ",")
}

func (f stringListFlag) Set(v string) error {
	*f.values = append(*f.values, v)
	return nil
}

func runEnroll(args []string) error {
	fs := flagSetWithHelp("enroll", enrollUsage)
	opts := enrollOptions{}
	fs.StringVar(&opts.issuer, "issuer", "", "")
	fs.StringVar(&opts.gateway, "gateway", "", "")
	fs.StringVar(&opts.cerea, "cerea", "", "")
	fs.StringVar(&opts.clientID, "client-id", "opencode-enrollment", "")
	fs.BoolVar(&opts.device, "device", false, "")
	fs.BoolVar(&opts.loopback, "loopback", false, "")
	fs.StringVar(&opts.group, "group", "", "")
	fs.StringVar(&opts.output, "output", "./opencode.json", "")
	fs.StringVar(&opts.creds, "creds", "", "")
	fs.IntVar(&opts.shimPort, "shim-port", defaultShimPort, "")
	// --no-discover is separate from --discover because the stdlib flag
	// package cannot negate one bool with another name on the same variable.
	noDiscover := fs.Bool("no-discover", false, "")
	fs.BoolVar(&opts.discover, "discover", true, "")
	fs.BoolVar(&opts.allowOpencodeProviders, "allow-opencode-provider", false, "")
	fs.BoolVar(&opts.allowAutoAccept, "allow-auto-accept", false, "")
	fs.Var(stringListFlag{&opts.workspaceRoots}, "workspace-root", "")
	fs.BoolVar(&opts.allowFreeModels, "allow-free-models", false, "")
	fs.BoolVar(&opts.yes, "yes", false, "")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if *noDiscover {
		opts.discover = false
	}
	if fs.NArg() > 0 {
		return fmt.Errorf("unexpected arguments: %s", strings.Join(fs.Args(), " "))
	}
	if opts.device && opts.loopback {
		return fmt.Errorf("--device and --loopback conflict: pick one flow")
	}
	if opts.creds == "" {
		path, err := defaultCredsPath()
		if err != nil {
			return err
		}
		opts.creds = path
	}
	return enroll(context.Background(), &opts)
}

func enroll(ctx context.Context, opts *enrollOptions) error {
	issuer := opts.issuer
	if issuer == "" {
		answer, err := promptRequired("OIDC issuer URL (where discovery lives): ")
		if err != nil {
			return err
		}
		issuer = answer
	}
	issuer = strings.TrimSuffix(issuer, "/")

	gateway, err := normalizeGateway(opts.gateway)
	if err != nil {
		return err
	}

	doc, err := fetchDiscovery(ctx, issuer)
	if err != nil {
		return err
	}

	useDevice, err := selectFlow(doc, opts.device, opts.loopback)
	if err != nil {
		return err
	}
	var tokens *tokenSet
	if useDevice {
		fmt.Fprintln(os.Stderr, "no usable browser here — signing in with the device code flow.")
		tokens, err = runDeviceFlow(ctx, doc, opts.clientID)
	} else {
		tokens, err = runLoopbackFlow(ctx, doc, opts.clientID)
	}
	if err != nil {
		return err
	}
	if tokens.RefreshToken == "" {
		return fmt.Errorf("the IdP issued no refresh token: offline access is required for the serve shim")
	}

	group, err := pickGroup(ctx, gateway, tokens.AccessToken, opts.group)
	if err != nil {
		return err
	}

	var models []gatewayModel
	if opts.discover {
		models = fetchModels(ctx, gateway, tokens.AccessToken)
		if len(models) == 0 {
			fmt.Fprintln(os.Stderr, "warning: model discovery failed; writing a placeholder models map — replace it with ids from GET /v1/models once reachable.")
		} else {
			fmt.Fprintf(os.Stderr, "discovered %d model(s) via GET %s/models\n", len(models), gateway)
		}
	}

	shimAddr, shimPort, err := pickShimAddr(opts.shimPort)
	if err != nil {
		return err
	}

	// C3: a fresh per-install secret every enroll, never reused across
	// machines or re-enrollments — opencode's config and this one gateway
	// bearer are the only two places it is ever written.
	shimSecret, err := randomHex(32)
	if err != nil {
		return fmt.Errorf("minting shim secret: %w", err)
	}

	if err := confirmOverwrite(opts.output, opts.creds, opts.yes); err != nil {
		return err
	}

	creds := &credentials{
		Issuer:        issuer,
		TokenEndpoint: doc.TokenEndpoint,
		ClientID:      opts.clientID,
		Gateway:       gateway,
		Group:         group,
		RefreshToken:  tokens.RefreshToken,
		AccessToken:   tokens.AccessToken,
		ExpiresIn:     tokens.ExpiresIn,
		ObtainedAt:    time.Now().Unix(),
		ShimPort:      shimPort,
		ShimSecret:    shimSecret,
		CereaOrigin:   strings.TrimSuffix(opts.cerea, "/"),
	}
	if err := saveCredentials(opts.creds, creds); err != nil {
		return err
	}
	if err := writeOpencodeConfig(
		opts.output,
		buildOpencodeConfig(shimAddr, shimSecret, models, opts.allowOpencodeProviders),
	); err != nil {
		return err
	}

	// The machine's own veto (PROTOCOL.md §4): written once here, never
	// writable over the link. `run` loads it from the same directory as
	// the credential file.
	pol := policy.Default()
	if opts.allowAutoAccept {
		pol.AutoAccept = policy.AutoAcceptAllowed
	}
	pol.WorkspaceRoots = opts.workspaceRoots
	pol.AllowFreeModels = opts.allowFreeModels
	if err := policy.Save(policyPathFor(opts.creds), pol); err != nil {
		return err
	}
	// Enrolling is a new identity for Cerea too: a fresh machine id means the
	// machine appears as a new pending device to confirm, and a machine revoked
	// in the panel can come back at all (its old id is refused for good).
	// 'run' keeps its state next to the credentials by default.
	if _, err := rotateMachineID(filepath.Dir(opts.creds)); err != nil {
		return fmt.Errorf("minting a new machine id: %w", err)
	}

	fmt.Fprintf(os.Stderr, "wrote %s (provider pystino via shim %s) and %s\n", opts.output, shimAddr, opts.creds)
	fmt.Fprintf(os.Stderr, "billing group: %s (sent as x-bill-to by the shim)\n", group)
	fmt.Fprintf(os.Stderr, "next: run 'pystino-agent run', then confirm this machine in the chat's /code panel.\n")
	fmt.Fprintf(os.Stderr, "      (for opencode on its own without the panel, 'pystino-agent serve' runs just the gateway shim.)\n")
	fmt.Fprintf(os.Stderr, "spend lands in the gateway's ledger under the billing group above; see it in the console.\n")
	return nil
}

// normalizeGateway accepts an origin or a /v1 base and returns the /v1 base.
// Users paste whatever the console shows them; a missing /v1 would otherwise
// send chat completions at the wrong path, and a trailing slash would double
// one onto every request the shim forwards.
func normalizeGateway(raw string) (string, error) {
	if raw == "" {
		answer, err := promptRequired("Gateway origin or /v1 base (e.g. https://llm.example.org): ")
		if err != nil {
			return "", err
		}
		raw = answer
	}
	raw = strings.TrimSuffix(strings.TrimSpace(raw), "/")
	if !strings.HasPrefix(raw, "http://") && !strings.HasPrefix(raw, "https://") {
		return "", fmt.Errorf("gateway must be an absolute http(s) URL: %s", raw)
	}
	if !strings.HasSuffix(raw, "/v1") {
		raw += "/v1"
		fmt.Fprintf(os.Stderr, "note: using %s (/v1 appended)\n", raw)
	}
	return raw, nil
}

// selectFlow picks device vs loopback. Explicit flags win; otherwise a
// display plus an authorization endpoint means loopback, and headless means
// the device flow — which then requires the IdP to advertise its endpoint.
func selectFlow(doc *discovery, forceDevice, forceLoopback bool) (bool, error) {
	switch {
	case forceDevice:
		if doc.DeviceAuthorizationEndpoint == "" {
			return false, fmt.Errorf("device flow requested but discovery names no device authorization endpoint")
		}
		return true, nil
	case forceLoopback:
		if doc.AuthorizationEndpoint == "" {
			return false, fmt.Errorf("loopback flow requested but discovery names no authorization endpoint")
		}
		return false, nil
	case hasDisplay() && doc.AuthorizationEndpoint != "":
		return false, nil
	case doc.DeviceAuthorizationEndpoint != "":
		return true, nil
	case doc.AuthorizationEndpoint != "":
		return false, fmt.Errorf("no display detected and no device endpoint advertised: re-run with --loopback on a machine with a browser")
	default:
		return false, fmt.Errorf("discovery names neither an authorization nor a device endpoint")
	}
}

// pickGroup resolves the x-bill-to value: a name, per the gateway's rule
// (ADR 0061). An explicit --group must be a current membership (verified
// against the live list, not trusted blind); several groups without a flag
// prompt; one group is taken silently but reported.
func pickGroup(ctx context.Context, gateway, accessToken, preselect string) (string, error) {
	groups, err := fetchGroups(ctx, gateway, accessToken)
	if err != nil {
		return "", err
	}
	names := make([]string, len(groups))
	for i, g := range groups {
		names[i] = g.Name
		if g.IsDefault {
			names[i] += " (default)"
		}
	}
	if preselect != "" {
		for _, g := range groups {
			if g.Name == preselect {
				return g.Name, nil
			}
		}
		return "", fmt.Errorf("group %q is not billable for this user (choices: %s)", preselect, strings.Join(names, ", "))
	}
	if len(groups) == 1 {
		fmt.Fprintf(os.Stderr, "billing group: %s (only one available)\n", groups[0].Name)
		return groups[0].Name, nil
	}
	fmt.Fprintln(os.Stderr, "billable groups:")
	def := 0
	for i, g := range groups {
		if g.IsDefault {
			def = i
		}
	}
	choice, err := promptChoice(fmt.Sprintf("pick a billing group [1-%d, default %d]: ", len(groups), def+1), names, def)
	if err != nil {
		return "", err
	}
	return groups[choice].Name, nil
}

// pickShimAddr finds the loopback address serve will listen on: the
// preferred port when free, else the next free one up. Binding here (and
// closing) only probes — serve binds it for real — so a fully busy range
// errors loudly instead of writing a config at a dead address.
func pickShimAddr(preferred int) (string, int, error) {
	for port := preferred; port < preferred+100; port++ {
		addr := fmt.Sprintf("127.0.0.1:%d", port)
		listener, err := net.Listen("tcp", addr)
		if err != nil {
			continue
		}
		_ = listener.Close()
		if port != preferred {
			fmt.Fprintf(os.Stderr, "note: port %d busy, using %s for the shim\n", preferred, addr)
		}
		return addr, port, nil
	}
	return "", 0, fmt.Errorf("no free port near %d for the serve shim", preferred)
}

// confirmOverwrite guards the two files enroll writes. --yes (or a
// non-terminal stdin answering yes) skips the question; anything else aborts
// rather than merging into files it did not create.
func confirmOverwrite(output, creds string, yes bool) error {
	targets := []string{}
	for _, path := range []string{output, creds} {
		if _, err := os.Stat(path); err == nil {
			targets = append(targets, path)
		}
	}
	if len(targets) == 0 || yes {
		return nil
	}
	answer, err := promptLine(fmt.Sprintf("%s exists; overwrite? [y/N] ", strings.Join(targets, ", ")))
	if err != nil {
		return err
	}
	if answer != "y" && answer != "Y" && answer != "yes" {
		return fmt.Errorf("aborted")
	}
	return nil
}
