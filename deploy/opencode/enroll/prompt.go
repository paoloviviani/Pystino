package main

import (
	"bufio"
	"fmt"
	"os"
	"strconv"
	"strings"
)

// promptLine reads one trimmed line from stdin. A closed pipe is a refusal,
// not an empty answer: callers that accept a default check for "" themselves.
func promptLine(prompt string) (string, error) {
	fmt.Fprintf(os.Stderr, "%s", prompt)
	line, err := bufio.NewReader(os.Stdin).ReadString('\n')
	if err != nil {
		return "", fmt.Errorf("reading input: %w", err)
	}
	return strings.TrimSpace(line), nil
}

// promptRequired keeps asking until it gets a non-empty answer.
func promptRequired(prompt string) (string, error) {
	for {
		answer, err := promptLine(prompt)
		if err != nil {
			return "", err
		}
		if answer != "" {
			return answer, nil
		}
	}
}

// promptChoice presents a numbered list and returns the chosen index.
// An empty answer picks def (a zero-based index); anything unparseable or
// out of range is re-asked rather than guessed at.
func promptChoice(prompt string, options []string, def int) (int, error) {
	for i, opt := range options {
		fmt.Fprintf(os.Stderr, "  %d) %s\n", i+1, opt)
	}
	for {
		answer, err := promptLine(prompt)
		if err != nil {
			return 0, err
		}
		if answer == "" {
			return def, nil
		}
		n, convErr := strconv.Atoi(answer)
		if convErr != nil || n < 1 || n > len(options) {
			fmt.Fprintf(os.Stderr, "enter a number 1-%d\n", len(options))
			continue
		}
		return n - 1, nil
	}
}
