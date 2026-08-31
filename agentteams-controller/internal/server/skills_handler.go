package server

import (
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"

	v1beta1 "github.com/agentscope-ai/AgentTeams/agentteams-controller/api/v1beta1"
	"github.com/agentscope-ai/AgentTeams/agentteams-controller/internal/httputil"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// builtinSkillAgents lists the agent template directories whose skills/ subdirs
// form the built-in catalog. It mirrors Deployer.builtinAgentDir exactly: the
// default worker template, the copaw and hermes runtime variants, and the
// team-leader template.
var builtinSkillAgents = []string{
	"", // default worker template (WorkerAgentDir itself)
	"copaw-worker-agent",
	"hermes-worker-agent",
	"team-leader-agent",
}

// SkillInfo is one entry of the read-only skill catalog.
type SkillInfo struct {
	Name        string   `json:"name"`
	Description string   `json:"description,omitempty"`
	Source      string   `json:"source"`           // "builtin" or the remote registry source (e.g. "nacos")
	Agents      []string `json:"agents,omitempty"` // builtin only: templates providing the skill
}

// SkillListResponse is the payload of GET /api/v1/skills.
type SkillListResponse struct {
	Skills []SkillInfo `json:"skills"`
	Total  int         `json:"total"`
}

// SkillsHandler serves the read-only skill catalog: built-in skills from the
// controller's agent template directories plus remote skill names already
// referenced by workers' spec.remoteSkills. It never reads skill content
// beyond the SKILL.md frontmatter (name/description).
type SkillsHandler struct {
	client         client.Client
	namespace      string
	workerAgentDir string
}

func NewSkillsHandler(c client.Client, namespace, workerAgentDir string) *SkillsHandler {
	return &SkillsHandler{client: c, namespace: namespace, workerAgentDir: workerAgentDir}
}

// ListSkills handles GET /api/v1/skills.
func (h *SkillsHandler) ListSkills(w http.ResponseWriter, r *http.Request) {
	skills := map[string]*SkillInfo{}

	for _, agent := range h.builtinAgentDirs() {
		skillRoot := filepath.Join(agent, "skills")
		entries, err := os.ReadDir(skillRoot)
		if err != nil {
			continue // missing template dir for this deployment
		}
		agentName := filepath.Base(agent)
		for _, entry := range entries {
			if !entry.IsDir() {
				continue
			}
			name, description := parseSkillFrontmatter(filepath.Join(skillRoot, entry.Name(), "SKILL.md"))
			if name == "" {
				name = entry.Name()
			}
			if info, ok := skills[name]; ok {
				if info.Source == "builtin" {
					info.Agents = appendUniqueStrings(info.Agents, agentName)
					if info.Description == "" {
						info.Description = description
					}
				}
				continue
			}
			skills[name] = &SkillInfo{Name: name, Description: description, Source: "builtin", Agents: []string{agentName}}
		}
	}

	if h.client != nil {
		var workers v1beta1.WorkerList
		if err := h.client.List(r.Context(), &workers, client.InNamespace(h.namespace)); err == nil {
			for i := range workers.Items {
				for _, src := range workers.Items[i].Spec.RemoteSkills {
					for _, sk := range src.Skills {
						if sk.Name == "" {
							continue
						}
						if info, ok := skills[sk.Name]; ok {
							// A builtin skill can also be refreshed from a
							// registry; keep the builtin entry and record the
							// registry source so clients know both exist.
							if info.Source != src.Source {
								info.Source = info.Source + "+" + src.Source
							}
							continue
						}
						skills[sk.Name] = &SkillInfo{Name: sk.Name, Source: src.Source}
					}
				}
			}
		}
	}

	list := make([]SkillInfo, 0, len(skills))
	for _, info := range skills {
		sort.Strings(info.Agents)
		list = append(list, *info)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].Name < list[j].Name })

	httputil.WriteJSON(w, http.StatusOK, SkillListResponse{Skills: list, Total: len(list)})
}

// builtinAgentDirs resolves the template directories that exist for this
// deployment, mirroring Deployer.builtinAgentDir.
func (h *SkillsHandler) builtinAgentDirs() []string {
	if h.workerAgentDir == "" {
		return nil
	}
	base := filepath.Dir(h.workerAgentDir)
	seen := map[string]struct{}{}
	dirs := make([]string, 0, len(builtinSkillAgents))
	for _, name := range builtinSkillAgents {
		dir := h.workerAgentDir
		if name != "" {
			dir = filepath.Join(base, name)
		}
		if _, ok := seen[dir]; ok {
			continue
		}
		seen[dir] = struct{}{}
		dirs = append(dirs, dir)
	}
	return dirs
}

// parseSkillFrontmatter extracts name/description from the YAML frontmatter
// of a SKILL.md. Returns ("", "") when the file or frontmatter is missing —
// callers fall back to the directory name.
func parseSkillFrontmatter(path string) (name, description string) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", ""
	}
	lines := strings.Split(string(data), "\n")
	if len(lines) == 0 || strings.TrimSpace(lines[0]) != "---" {
		return "", ""
	}
	for _, line := range lines[1:] {
		trimmed := strings.TrimSpace(line)
		if trimmed == "---" {
			break
		}
		if v, ok := strings.CutPrefix(trimmed, "name:"); ok {
			name = strings.TrimSpace(v)
		} else if v, ok := strings.CutPrefix(trimmed, "description:"); ok {
			description = strings.TrimSpace(v)
		}
	}
	return name, description
}

func appendUniqueStrings(list []string, s string) []string {
	for _, v := range list {
		if v == s {
			return list
		}
	}
	return append(list, s)
}
