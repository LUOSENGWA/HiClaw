package server

import (
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/gateway"
)

// modelsListStub implements only ListAIRoutes; any other interface call
// panics via the embedded nil interface, so a test that reaches the wrong
// method fails loudly.
type modelsListStub struct {
	gateway.Client
	routes []gateway.AIRouteInfo
	err    error
}

func (s *modelsListStub) ListAIRoutes(ctx context.Context) ([]gateway.AIRouteInfo, error) {
	return s.routes, s.err
}

func TestGatewayHandler_ListModels(t *testing.T) {
	cases := []struct {
		name       string
		gw         gateway.Client
		nilGW      bool
		wantStatus int
		wantBody   string
	}{
		{
			name: "ok",
			gw: &modelsListStub{routes: []gateway.AIRouteInfo{
				{Name: "model-a", AllowedConsumers: []string{"manager"}},
			}},
			wantStatus: http.StatusOK,
			wantBody:   `"total":1`,
		},
		{
			name:       "empty catalog",
			gw:         &modelsListStub{routes: nil},
			wantStatus: http.StatusOK,
			wantBody:   `"models":[]`,
		},
		{
			name:       "unsupported backend",
			gw:         &modelsListStub{err: gateway.ErrUnsupportedOp},
			wantStatus: http.StatusNotImplemented,
			wantBody:   "not supported",
		},
		{
			name:       "upstream error",
			gw:         &modelsListStub{err: errors.New("boom")},
			wantStatus: http.StatusBadGateway,
			wantBody:   "boom",
		},
		{
			name:       "no gateway configured",
			nilGW:      true,
			wantStatus: http.StatusNotImplemented,
			wantBody:   "no gateway backend available",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var h *GatewayHandler
			if tc.nilGW {
				h = &GatewayHandler{}
			} else {
				h = NewGatewayHandler(tc.gw)
			}
			rec := httptest.NewRecorder()
			h.ListModels(rec, httptest.NewRequest(http.MethodGet, "/api/v1/models", nil))
			if rec.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d (body %s)", rec.Code, tc.wantStatus, rec.Body.String())
			}
			if !strings.Contains(rec.Body.String(), tc.wantBody) {
				t.Fatalf("body = %s, want to contain %s", rec.Body.String(), tc.wantBody)
			}
		})
	}
}
