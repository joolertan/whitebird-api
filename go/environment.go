package whitebird

import (
	"fmt"
	"net/url"
	"slices"
	"strings"
)

// Environment selects the hosts a [Client] talks to: [Production] (the default) or the [Demo]
// trading sandbox. Services without a host of their own in an environment use their production
// host.
type Environment string

// Service is the key of a Whitebird backend service. Every service has its own host, which
// [WithBaseURL] can override.
type Service string

func resolveBaseURLs(env Environment, overrides map[Service]string) (map[Service]string, error) {
	if !slices.Contains(environments, env) {
		return nil, fmt.Errorf("whitebird: unknown environment %q", env)
	}
	urls := make(map[Service]string, len(serviceURLs)+len(overrides))
	for service, hosts := range serviceURLs {
		if host, ok := hosts[env]; ok {
			urls[service] = host
		} else {
			urls[service] = hosts[defaultEnvironment]
		}
	}
	for service, raw := range overrides {
		u, err := url.Parse(raw)
		if err != nil || u.Scheme == "" || u.Host == "" {
			return nil, fmt.Errorf("whitebird: invalid base URL %q for service %q", raw, service)
		}
		urls[service] = strings.TrimSuffix(raw, "/")
	}
	return urls, nil
}
