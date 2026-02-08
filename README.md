# Helios SBC Service

Helios SBC Service is a robust and efficient edge service designed to manage and monitor drone telemetry data. This service provides real-time data streaming, telemetry visualization, and RESTful APIs for seamless integration with other systems.

## Features

- **Real-time Telemetry Streaming**: Stream live telemetry data from drones, including position, attitude, and battery status.
- **RESTful APIs**: Access telemetry data through easy-to-use RESTful endpoints.
- **Data Visualization**: Visualize telemetry data in an intuitive and user-friendly interface.
- **Customizable Fields**: Choose specific telemetry fields to monitor, such as position, attitude, or battery.

## Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/see-k/helios-sbc-service.git
   ```
2. Navigate to the project directory:
   ```bash
   cd helios-sbc-service
   ```
3. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   .venv\Scripts\activate  # On Windows
   source .venv/bin/activate  # On macOS/Linux
   ```
4. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Usage

1. Start the service:
   ```bash
   python app.py
   ```
2. Run the WebSocket test script:
   ```bash
   python test_ws.py
   ```
3. Access the telemetry data via the RESTful API:
   - Example: `http://localhost:5000/api/telemetry/battery` to get battery data.

## API Endpoints

- **`GET /api/telemetry/battery`**: Retrieve the latest battery telemetry data.
- **`GET /api/telemetry/position`**: Retrieve the latest position data.
- **`GET /api/telemetry/attitude`**: Retrieve the latest attitude data.

## Contributing

Contributions are welcome! Please follow these steps:

1. Fork the repository.
2. Create a new branch for your feature or bug fix:
   ```bash
   git checkout -b feature-name
   ```
3. Commit your changes:
   ```bash
   git commit -m "Description of changes"
   ```
4. Push to your branch:
   ```bash
   git push origin feature-name
   ```
5. Open a pull request.

## License

This project is licensed under the MIT License. See the LICENSE file for details.

## Contact

For any inquiries or support, please contact [see-k](https://github.com/see-k).
