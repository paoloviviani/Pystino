package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
)

// billableGroup is one entry of GET /v1/billing/groups (ADR 0061): the set a
// bearer caller may name in x-bill-to. The header takes the name, not the id,
// so the name is what enroll records.
type billableGroup struct {
	ID        string `json:"id"`
	Name      string `json:"name"`
	IsDefault bool   `json:"is_default"`
}

// fetchGroups lists the groups the access token may bill. A 401/403 here is
// an auth verdict on the token just minted, and a 429 is the quota door —
// both are reported as what they are rather than as a generic failure.
func fetchGroups(ctx context.Context, gateway, accessToken string) ([]billableGroup, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimSuffix(gateway, "/")+"/billing/groups", nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Authorization", "Bearer "+accessToken)
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("listing billing groups: %w", err)
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusOK:
	case http.StatusUnauthorized, http.StatusForbidden:
		return nil, fmt.Errorf("gateway refused the token (HTTP %d): sign in again", resp.StatusCode)
	case http.StatusTooManyRequests:
		return nil, fmt.Errorf("gateway reports quota exhausted (HTTP 429): wait for the next window or raise the cap, then re-run")
	default:
		return nil, fmt.Errorf("billing groups answered HTTP %d", resp.StatusCode)
	}
	var listing struct {
		Data []billableGroup `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&listing); err != nil {
		return nil, fmt.Errorf("parsing billing groups: %w", err)
	}
	if len(listing.Data) == 0 {
		return nil, fmt.Errorf("the gateway lists no billable groups for this user")
	}
	return listing.Data, nil
}

// gatewayModel is one entry of GET /v1/models: the OpenAI shape plus the
// gateway's limit hints and kind, which become opencode's model entries.
type gatewayModel struct {
	ID              string `json:"id"`
	DisplayName     string `json:"display_name"`
	ContextWindow   int    `json:"context_window"`
	MaxOutputTokens int    `json:"max_output_tokens"`
	Kind            string `json:"kind"`
}

// fetchModels discovers the caller's catalogue for the opencode models map.
// It never fails enroll: an unreachable gateway still yields a working auth
// setup, with a placeholder the user replaces once the gateway is reachable.
func fetchModels(ctx context.Context, gateway, accessToken string) []gatewayModel {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimSuffix(gateway, "/")+"/models", nil)
	if err != nil {
		return nil
	}
	req.Header.Set("Authorization", "Bearer "+accessToken)
	resp, err := httpClient.Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	var listing struct {
		Data []gatewayModel `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&listing); err != nil {
		return nil
	}
	return listing.Data
}
