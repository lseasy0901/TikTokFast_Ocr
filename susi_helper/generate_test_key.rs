use susi_core::crypto;

fn main() -> Result<(), Box<dyn std::error::Error>> {
    println!("Generating test RSA keypair for Susi POC...");

    // Generate 2048-bit keypair
    let (private_key, public_key) = crypto::generate_keypair(2048)?;

    // Export to PEM
    let private_pem = crypto::private_key_to_pem(&private_key)?;
    let public_pem = crypto::public_key_to_pem(&public_key)?;

    // Write to files
    std::fs::write("private_test_key.pem", private_pem)?;
    std::fs::write("public_test_key.pem", public_pem)?;

    println!("✓ Test keypair generated successfully:");
    println!("- private_test_key.pem (DO NOT commit this to repository!)");
    println!("- public_test_key.pem (safe to use in client)");
    println!();
    println!("Public key content:");
    println!("{}", public_pem);

    Ok(())
}