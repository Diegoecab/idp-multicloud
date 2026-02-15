# Crossplane Artifacts

This folder contains starter Crossplane manifests to make the control-plane integration explicit.

## What is here
- `mysqlinstanceclaim-crd.yaml`: An example `CompositeResourceDefinition` exposing
  `MySQLInstance` and namespaced `MySQLInstanceClaim` for
  `db.platform.example.org/v1alpha1`.
- `composition-aws.yaml`: Example composition selected by label
  `db.platform.example.org/provider: aws`.
- `composition-gcp.yaml`: Example composition selected by label
  `db.platform.example.org/provider: gcp`.
- `composition-oci.yaml`: Example composition selected by label
  `db.platform.example.org/provider: oci`.

## Label contract
The control-plane creates claims with:

```yaml
spec:
  compositionSelector:
    matchLabels:
      db.platform.example.org/provider: <aws|gcp|oci>
      db.platform.example.org/class: mysql
```

Your compositions must include matching labels.

## Apply order
1. Install Crossplane and provider packages.
2. Apply `mysqlinstanceclaim-crd.yaml`.
3. Apply all provider compositions.
4. Start the control-plane API and create claims.
