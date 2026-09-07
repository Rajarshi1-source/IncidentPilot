---
name: Certificate Expiry
alert_pattern: "CertExpiry|TLSCertExpiring|CertificateNotAfter"
severity_filter: sev3
service_filter: null
version: 1.0.0
---

A TLS certificate is close to expiry, or has expired. This is the one alert class
where the deadline is known in advance and the outage is entirely avoidable —
which is also why the PIR action item usually turns out to be about renewal
automation rather than about the certificate.

<!-- step:identify-cert -->
### Identify exactly which certificate

```bash
kubectl -n {{namespace}} get certificate,secret -l app={{service}}
echo | openssl s_client -connect {{service}}.{{namespace}}:443 2>/dev/null \
  | openssl x509 -noout -subject -dates
```

Serving certificates, client certificates and the mTLS pair used between services
expire independently. Renewing the wrong one costs a rollout and leaves the alert
firing.

<!-- step:check-issuer -->
### Check the issuer is healthy

```bash
kubectl -n {{namespace}} describe certificate {{service}}-tls | tail -30
kubectl -n cert-manager logs deploy/cert-manager --tail=100 | grep -i error
```

An expiring certificate with a working issuer is a renewal that has not run yet.
An expiring certificate with a *broken* issuer is the real incident, and it will
take every other certificate with it on their own schedules.

<!-- step:renew-or-reissue -->
### Renew, or reissue

```bash
kubectl -n {{namespace}} annotate certificate {{service}}-tls \
  cert-manager.io/issue-temporary-certificate="true" --overwrite
kubectl -n {{namespace}} delete secret {{service}}-tls   # forces a reissue
```

Deleting the secret is safe only while the issuer is healthy. Confirm the
previous step first — otherwise this turns a warning into an outage.

<!-- step:roll-pods -->
### Roll the pods that hold it

```bash
kubectl -n {{namespace}} rollout restart deploy/{{service}}
```

Most servers read the certificate once at startup. A renewed secret with no
restart is a renewed secret nothing is using.

<!-- step:verify-chain -->
### Verify the whole chain

```bash
echo | openssl s_client -connect {{service}}.{{namespace}}:443 -showcerts 2>/dev/null \
  | openssl x509 -noout -dates -issuer
```

Check the intermediate as well as the leaf. A leaf that is valid behind an
expired intermediate fails for clients and validates for whoever is testing with
`-k`, which is a long afternoon.
