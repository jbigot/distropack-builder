# Rotate the signing key

Users trust a repository through the public key they installed: a new key must reach them before
packages signed with it do.

1. Generate the new key, passphrase-protected, with a user id naming the repository, for example
   with `sq key generate` or `gpg --quick-generate-key`, and export its secret part to a file.
2. Publish its public part first, signed by the old key where you can, so that users can check it
   came from you: for deb, a new version of the archive keyring package holding both keys (apt
   accepts a `Signed-By` file with several keys); for rpm, both keys in the `.key` file, which dnf
   imports at once.
3. Once that version is installed widely enough, point `build.conf`'s `gpg.file` (or `gpg.id`,
   when the file holds both) at the new key, and run the builder with the new passphrase: every
   release it publishes is signed with the new key, and the public key files it writes
   (`<name>-archive-keyring.gpg`, `<name>.key`) hold the new key alone.
4. Releases a run does not publish keep their old signatures until they are rebuilt: rebuild them
   all before retiring the old key from the keyring package.

Signing itself is described in [](../explanation/signing.md).
