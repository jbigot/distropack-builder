# Rotate the signing key

Users trust a repository through the public key they installed: a new key must reach them before
packages signed with it do.

1. Generate the new key, passphrase-protected, with a user id naming the repository, for example
   with `sq key generate` or `gpg --quick-generate-key`, and export its secret part to a file.
2. For deb, publish its public part first: a new version of the archive keyring package holding
   both keys (apt accepts a `Signed-By` file with several keys), so that users have the new key
   before anything is signed with it.
3. Once that version is installed widely enough, point `build.conf`'s `gpg.file` (or `gpg.id`,
   when the file holds both) at the new key, and run the builder with the new passphrase: every
   release it publishes is signed with the new key, and the public key files it writes
   (`<name>-archive-keyring.gpg`, `<name>.key`) hold the new key alone. dnf users get it from the
   `gpgkey` URL of their `.repo` file, which points at `<name>.key`: dnf asks them to accept the
   new key the first time it meets a signature it cannot check, so say so wherever users learn
   about the rotation.
4. Releases a run does not publish keep their old signatures until they are rebuilt: rebuild them
   all before retiring the old key from the keyring package.

Signing itself is described in [](../explanation/signing.md).
