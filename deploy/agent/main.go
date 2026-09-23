// Command pystino-agent authenticates a human to the Pystino gateway through
// the bundled IdP (ADR 0084), wires up opencode, and (via `run`) supervises
// opencode and a WSS link to Cerea that drives coding sessions through this
// machine. See PROTOCOL.md for the wire protocol `run` speaks to Cerea.
//
// An OIDC access token expires in minutes (ADR 0040 contrasts API keys as
// "not expiring in five minutes"), while opencode holds a static apiKey. So
// `enroll` authenticates once and records how to renew, and `serve` is the
// local shim that owns the renewal so opencode never sees it. `run` grows
// out of `serve`: it starts the shim, supervises `opencode serve`, and dials
// out to Cerea.
package main

import (
	"flag"
	"fmt"
	"os"
)

const usage = `pystino-agent — authenticate to the Pystino gateway and run opencode for Cerea.

Usage:
  pystino-agent <command> [options]

Commands:
  enroll    Run the OAuth flow, pick a billing group, write opencode.json
            and store the refresh credential.
  serve     Local refreshing proxy shim: opencode points its baseURL here,
            the shim injects a fresh access token plus x-bill-to per request.

Run 'pystino-agent <command> -h' for that command's options.
`

func main() {
	if len(os.Args) < 2 {
		fmt.Fprint(os.Stderr, usage)
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "enroll":
		err = runEnroll(os.Args[2:])
	case "serve":
		err = runServe(os.Args[2:])
	case "-h", "-help", "--help", "help":
		fmt.Print(usage)
		return
	default:
		fmt.Fprintf(os.Stderr, "unknown command %q\n\n%s", os.Args[1], usage)
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n", err)
		os.Exit(1)
	}
}

// flagSetWithHelp builds a FlagSet that reports errors like the stdlib but
// prints our own usage text, so every subcommand documents itself the same way.
func flagSetWithHelp(name string, usageText string) *flag.FlagSet {
	fs := flag.NewFlagSet(name, flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	fs.Usage = func() { fmt.Fprint(os.Stderr, usageText) }
	return fs
}
