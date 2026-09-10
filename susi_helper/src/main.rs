use susi_core::{
    crypto::{self, sign_license, verify_license, public_key_from_pem},
    fingerprint,
    license::{LicensePayload, SignedLicense},
};
use std::io::{self, Read};

#[derive(serde::Deserialize, Debug)]
#[serde(tag = "command")]
enum Command {
    GetMachineCode,
    Verify { signed_license: String },
    #[serde(skip)]
    Unknown,
}

#[derive(serde::Serialize, Debug)]
enum Response<T> {
    Success { data: T },
    Error { message: String },
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut stdin = io::stdin();
    let stdout = io::stdout();

    // Read JSON from stdin
    let mut input = String::new();
    stdin.read_to_string(&mut input)?;

    // Parse command
    let command: Command = serde_json::from_str(&input)
        .map_err(|e| format!("Invalid JSON: {}", e))?;

    let response = match command {
        Command::GetMachineCode => {
            match fingerprint::get_machine_code() {
                Ok(machine_code) => Response::Success { data: machine_code },
                Err(e) => Response::Error { message: format!("Failed to get machine code: {}", e) },
            }
        }
        Command::Verify { signed_license } => {
            let signed: SignedLicense = serde_json::from_str(&signed_license)
                .map_err(|e| format!("Invalid SignedLicense format: {}", e))?;

            // In a real implementation, we'd need the public key
            // For this POC, we'll just demonstrate the structure
            match verify_license_from_pem(&signed, "PUBLIC_KEY_HERE") {
                Ok(payload) => Response::Success { data: format!("Valid license: {}", payload.license_key) },
                Err(e) => Response::Error { message: format!("Verification failed: {}", e) },
            }
        }
        Command::Unknown => Response::Error { message: "Unknown command".to_string() },
    };

    // Write response to stdout
    let json_response = serde_json::to_string(&response)?;
    println!("{}", json_response);

    Ok(())
}

fn verify_license_from_pem(signed: &SignedLicense, pem: &str) -> Result<LicensePayload, String> {
    let public_key = public_key_from_pem(pem)
        .map_err(|e| format!("Invalid public key PEM: {}", e))?;

    verify_license(&public_key, signed)
        .map_err(|e| format!("License verification error: {}", e))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn test_machine_code_generation() {
        let result = fingerprint::get_machine_code();
        assert!(result.is_ok());
        let machine_code = result.unwrap();
        assert_eq!(machine_code.len(), 64);
        assert!(machine_code.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn test_license_sign_verify_roundtrip() {
        // Generate test keypair
        let (private_key, public_key) = crypto::generate_keypair(2048).unwrap();

        // Create test payload
        let payload = LicensePayload {
            id: "test-id".to_string(),
            product: "TestProduct".to_string(),
            customer: "TestCustomer".to_string(),
            license_key: "TEST-KEY-123".to_string(),
            created: chrono::Utc::now(),
            expires: None,
            features: vec!["feature1".to_string()],
            machine_codes: vec![],
            lease_expires: None,
            lease_grace_period: None,
            require_signed_binary: false,
        };

        // Sign license
        let signed = sign_license(&private_key, &payload).unwrap();

        // Verify license
        let verified = verify_license(&public_key, &signed).unwrap();

        // Verify payload integrity
        assert_eq!(verified.id, payload.id);
        assert_eq!(verified.product, payload.product);
        assert_eq!(verified.customer, payload.customer);
        assert_eq!(verified.license_key, payload.license_key);
        assert_eq!(verified.features, payload.features);
    }
}