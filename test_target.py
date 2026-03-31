from dash import Dash, html, dcc, Input, Output
import time

app = Dash(__name__)
app.layout = html.Div([
    html.Button('Click', id='btn'),
    html.Div('Hello', id='out'),
    dcc.Loading(
        custom_spinner=html.Div('LOADING...', style={'position': 'fixed', 'top': 0, 'left': 0, 'background': 'red', 'color': 'white', 'width': '100%'}),
        target_components={'out': 'children'}
    )
])

@app.callback(Output('out', 'children'), Input('btn', 'n_clicks'))
def update(n):
    if n:
        time.sleep(2)
        return 'Done'
    return 'Init'

if __name__ == '__main__':
    with open('test_target_out.txt', 'w') as f:
        f.write('OK')
